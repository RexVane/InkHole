//! Stable native boundary shared by Flutter and non-Tauri hosts.

use std::{
    any::Any,
    ffi::{CString, c_char},
    panic::{AssertUnwindSafe, catch_unwind},
    ptr, slice, str,
    sync::atomic::{AtomicBool, Ordering},
    time::Duration,
};

use inkhole_core::JsonService;
use tokio::runtime::{Builder, Runtime};

pub const INKHOLE_FFI_ABI_VERSION: u32 = 1;
pub const INKHOLE_STATUS_OK: i32 = 0;
pub const INKHOLE_STATUS_NO_EVENT: i32 = 1;
pub const INKHOLE_STATUS_INVALID_ARGUMENT: i32 = 2;
pub const INKHOLE_STATUS_INVALID_UTF8: i32 = 3;
pub const INKHOLE_STATUS_CLOSED: i32 = 4;
pub const INKHOLE_STATUS_INTERNAL_ERROR: i32 = 5;
pub const INKHOLE_STATUS_PANIC: i32 = 6;
/// Returned when a blocking entry point is called from inside the service runtime.
pub const INKHOLE_STATUS_WRONG_THREAD: i32 = 7;

const CLOSE_TIMEOUT: Duration = Duration::from_secs(10);
const RUNTIME_SHUTDOWN_TIMEOUT: Duration = Duration::from_secs(2);

/// Opaque service handle. Native callers only store and pass its pointer.
pub struct InkholeService {
    runtime: Option<Runtime>,
    service: JsonService,
    closed: AtomicBool,
}

struct BoundaryOutput {
    status: i32,
    value: Option<String>,
}

impl BoundaryOutput {
    fn ok(value: impl Into<String>) -> Self {
        Self {
            status: INKHOLE_STATUS_OK,
            value: Some(value.into()),
        }
    }

    fn empty() -> Self {
        Self {
            status: INKHOLE_STATUS_OK,
            value: None,
        }
    }

    fn error(status: i32, message: impl Into<String>) -> Self {
        Self {
            status,
            value: Some(message.into()),
        }
    }
}

/// Returns the version of this C ABI, independent from the JSON protocol version.
#[unsafe(no_mangle)]
pub extern "C" fn inkhole_ffi_abi_version() -> u32 {
    INKHOLE_FFI_ABI_VERSION
}

/// Creates a service and its multi-thread Tokio runtime.
///
/// # Safety
///
/// `out_service` and `out_message` must be valid writable pointers. Any non-null
/// message returned through `out_message` must be released with
/// [`inkhole_string_free`].
#[unsafe(no_mangle)]
pub unsafe extern "C" fn inkhole_service_create(
    out_service: *mut *mut InkholeService,
    out_message: *mut *mut c_char,
) -> i32 {
    ffi_boundary(out_message, || {
        if out_service.is_null() {
            return BoundaryOutput::error(
                INKHOLE_STATUS_INVALID_ARGUMENT,
                "out_service must not be null",
            );
        }
        // SAFETY: The caller guarantees that out_service is writable.
        unsafe { out_service.write(ptr::null_mut()) };
        let runtime = match Builder::new_multi_thread()
            .enable_all()
            .thread_name("inkhole-runtime")
            .build()
        {
            Ok(runtime) => runtime,
            Err(error) => {
                return BoundaryOutput::error(
                    INKHOLE_STATUS_INTERNAL_ERROR,
                    format!("could not create Tokio runtime: {error}"),
                );
            }
        };
        let service = Box::new(InkholeService {
            runtime: Some(runtime),
            service: JsonService::new(),
            closed: AtomicBool::new(false),
        });
        // SAFETY: The caller owns the returned allocation until destroy.
        unsafe { out_service.write(Box::into_raw(service)) };
        BoundaryOutput::empty()
    })
}

/// Calls the JSON request API synchronously on the service runtime.
///
/// A protocol-level failure is still `INKHOLE_STATUS_OK`; its details are in
/// the returned JSON response's `ok` and `error` fields.
///
/// # Safety
///
/// `service` must be a live handle returned by [`inkhole_service_create`].
/// `request` must reference `request_len` readable bytes, unless the length is
/// zero. `out_json` must be writable and its returned string must be freed.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn inkhole_service_call(
    service: *mut InkholeService,
    request: *const u8,
    request_len: usize,
    out_json: *mut *mut c_char,
) -> i32 {
    ffi_boundary(out_json, || {
        // SAFETY: Pointer validity is part of this function's caller contract.
        let service = match unsafe { service_ref(service) } {
            Ok(service) => service,
            Err(error) => return error,
        };
        if service.closed.load(Ordering::Acquire) {
            return BoundaryOutput::error(INKHOLE_STATUS_CLOSED, "service is closed");
        }
        // SAFETY: Pointer validity is part of this function's caller contract.
        let request = match unsafe { utf8_input(request, request_len) } {
            Ok(request) => request,
            Err(error) => return error,
        };
        let runtime = match service.runtime.as_ref() {
            Some(runtime) => runtime,
            None => {
                return BoundaryOutput::error(INKHOLE_STATUS_CLOSED, "service is closed");
            }
        };
        BoundaryOutput::ok(runtime.block_on(service.service.call_json(request)))
    })
}

/// Polls one JSON event. `timeout_ms` is `-1` for an infinite wait, `0` for a
/// non-blocking poll, or a positive timeout in milliseconds.
///
/// # Safety
///
/// `service` must be live and `out_event` must be writable. A returned string
/// must be freed with [`inkhole_string_free`].
#[unsafe(no_mangle)]
pub unsafe extern "C" fn inkhole_service_poll_event(
    service: *mut InkholeService,
    timeout_ms: i64,
    out_event: *mut *mut c_char,
) -> i32 {
    ffi_boundary(out_event, || {
        // SAFETY: Pointer validity is part of this function's caller contract.
        let service = match unsafe { service_ref(service) } {
            Ok(service) => service,
            Err(error) => return error,
        };
        let timeout = match timeout_from_millis(timeout_ms) {
            Ok(timeout) => timeout,
            Err(error) => return error,
        };
        let runtime = match service.runtime.as_ref() {
            Some(runtime) => runtime,
            None => {
                return BoundaryOutput::error(INKHOLE_STATUS_CLOSED, "service is closed");
            }
        };
        match runtime.block_on(service.service.poll_event(timeout)) {
            Some(event) => BoundaryOutput::ok(event),
            None if service.closed.load(Ordering::Acquire) => {
                BoundaryOutput::error(INKHOLE_STATUS_CLOSED, "service is closed")
            }
            None => BoundaryOutput {
                status: INKHOLE_STATUS_NO_EVENT,
                value: None,
            },
        }
    })
}

/// Stops all sessions while keeping the service reusable.
///
/// # Safety
///
/// `service` must be live and `out_message` must be writable.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn inkhole_service_reset(
    service: *mut InkholeService,
    out_message: *mut *mut c_char,
) -> i32 {
    ffi_boundary(out_message, || {
        // SAFETY: Pointer validity is part of this function's caller contract.
        let service = match unsafe { service_ref(service) } {
            Ok(service) => service,
            Err(error) => return error,
        };
        if service.closed.load(Ordering::Acquire) {
            return BoundaryOutput::error(INKHOLE_STATUS_CLOSED, "service is closed");
        }
        let runtime = match service.runtime.as_ref() {
            Some(runtime) => runtime,
            None => {
                return BoundaryOutput::error(INKHOLE_STATUS_CLOSED, "service is closed");
            }
        };
        match runtime.block_on(service.service.reset()) {
            Ok(()) => BoundaryOutput::empty(),
            Err(error) => BoundaryOutput::error(INKHOLE_STATUS_INTERNAL_ERROR, error.to_string()),
        }
    })
}

/// Gracefully closes the service. Repeated calls succeed.
///
/// # Safety
///
/// `service` must be live and `out_message` must be writable.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn inkhole_service_close(
    service: *mut InkholeService,
    out_message: *mut *mut c_char,
) -> i32 {
    ffi_boundary(out_message, || {
        // SAFETY: Pointer validity is part of this function's caller contract.
        let service = match unsafe { service_ref(service) } {
            Ok(service) => service,
            Err(error) => return error,
        };
        close_service(service)
    })
}

/// Closes and releases a service handle.
///
/// No other thread may be using `service` when this function is called. The
/// pointer is invalid after this call even when a non-zero status is returned.
///
/// # Safety
///
/// `service` must be null or a live, uniquely owned handle returned by create.
/// `out_message` must be writable.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn inkhole_service_destroy(
    service: *mut InkholeService,
    out_message: *mut *mut c_char,
) -> i32 {
    ffi_boundary(out_message, || {
        if service.is_null() {
            return BoundaryOutput::error(
                INKHOLE_STATUS_INVALID_ARGUMENT,
                "service must not be null",
            );
        }
        if let Some(error) = reject_runtime_thread() {
            return error;
        }
        // SAFETY: The caller transfers the unique allocation back to Rust.
        let mut service = unsafe { Box::from_raw(service) };
        let close_result = close_service(&service);
        let runtime = service.runtime.take();
        drop(service);
        if let Some(runtime) = runtime {
            runtime.shutdown_timeout(RUNTIME_SHUTDOWN_TIMEOUT);
        }
        close_result
    })
}

/// Releases a string returned by any other FFI function. Null is accepted.
///
/// # Safety
///
/// `value` must be null or a pointer returned by this library that has not
/// already been freed.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn inkhole_string_free(value: *mut c_char) {
    if value.is_null() {
        return;
    }
    let _ = catch_unwind(AssertUnwindSafe(|| {
        // SAFETY: Ownership is returned by the caller under this API contract.
        drop(unsafe { CString::from_raw(value) });
    }));
}

fn close_service(service: &InkholeService) -> BoundaryOutput {
    if let Some(error) = reject_runtime_thread() {
        return error;
    }
    if service.closed.swap(true, Ordering::AcqRel) {
        return BoundaryOutput::empty();
    }
    let runtime = match service.runtime.as_ref() {
        Some(runtime) => runtime,
        None => return BoundaryOutput::empty(),
    };
    match runtime
        .block_on(async { tokio::time::timeout(CLOSE_TIMEOUT, service.service.close()).await })
    {
        Ok(Ok(())) => BoundaryOutput::empty(),
        Ok(Err(error)) => BoundaryOutput::error(INKHOLE_STATUS_INTERNAL_ERROR, error.to_string()),
        Err(_) => BoundaryOutput::error(INKHOLE_STATUS_INTERNAL_ERROR, "service close timed out"),
    }
}

/// Blocking entry points would deadlock or abort when called from a task running
/// on the service runtime, so they refuse to run there.
fn reject_runtime_thread() -> Option<BoundaryOutput> {
    tokio::runtime::Handle::try_current().ok().map(|_| {
        BoundaryOutput::error(
            INKHOLE_STATUS_WRONG_THREAD,
            "close and destroy must not be called from a runtime thread",
        )
    })
}

fn timeout_from_millis(timeout_ms: i64) -> Result<Option<Duration>, BoundaryOutput> {
    match timeout_ms {
        -1 => Ok(None),
        0.. => Ok(Some(Duration::from_millis(timeout_ms as u64))),
        _ => Err(BoundaryOutput::error(
            INKHOLE_STATUS_INVALID_ARGUMENT,
            "timeout_ms must be -1 or non-negative",
        )),
    }
}

unsafe fn service_ref<'a>(
    service: *mut InkholeService,
) -> Result<&'a InkholeService, BoundaryOutput> {
    // SAFETY: The caller promises a live handle; null is checked here.
    unsafe { service.as_ref() }.ok_or_else(|| {
        BoundaryOutput::error(INKHOLE_STATUS_INVALID_ARGUMENT, "service must not be null")
    })
}

unsafe fn utf8_input<'a>(input: *const u8, input_len: usize) -> Result<&'a str, BoundaryOutput> {
    if input_len == 0 {
        return Ok("");
    }
    if input.is_null() {
        return Err(BoundaryOutput::error(
            INKHOLE_STATUS_INVALID_ARGUMENT,
            "request must not be null when request_len is non-zero",
        ));
    }
    // SAFETY: The caller promises that input_len bytes are readable.
    let bytes = unsafe { slice::from_raw_parts(input, input_len) };
    str::from_utf8(bytes).map_err(|error| {
        BoundaryOutput::error(
            INKHOLE_STATUS_INVALID_UTF8,
            format!("request is not valid UTF-8: {error}"),
        )
    })
}

fn ffi_boundary(out_value: *mut *mut c_char, operation: impl FnOnce() -> BoundaryOutput) -> i32 {
    if out_value.is_null() {
        return INKHOLE_STATUS_INVALID_ARGUMENT;
    }
    // SAFETY: Callers of exported functions guarantee this output is writable.
    unsafe { out_value.write(ptr::null_mut()) };
    let output = match catch_unwind(AssertUnwindSafe(operation)) {
        Ok(output) => output,
        Err(payload) => BoundaryOutput::error(
            INKHOLE_STATUS_PANIC,
            format!("Rust panic: {}", panic_message(payload)),
        ),
    };
    let mut status = output.status;
    if let Some(value) = output.value {
        let value = match CString::new(value) {
            Ok(value) => value,
            Err(_) => {
                status = INKHOLE_STATUS_INTERNAL_ERROR;
                CString::new("FFI output contained an interior NUL")
                    .expect("static error has no NUL")
            }
        };
        // SAFETY: out_value is writable and ownership transfers to the caller.
        unsafe { out_value.write(value.into_raw()) };
    }
    status
}

fn panic_message(payload: Box<dyn Any + Send>) -> String {
    if let Some(message) = payload.downcast_ref::<&str>() {
        (*message).to_owned()
    } else if let Some(message) = payload.downcast_ref::<String>() {
        message.clone()
    } else {
        "non-string panic payload".to_owned()
    }
}

#[cfg(test)]
mod tests {
    use std::{
        ffi::CStr,
        sync::{Arc, Barrier},
        thread,
    };

    use serde_json::Value;

    use super::*;

    unsafe fn take_output(value: *mut c_char) -> String {
        assert!(!value.is_null());
        // SAFETY: Tests only pass strings returned by this library.
        let output = unsafe { CStr::from_ptr(value) }
            .to_str()
            .unwrap()
            .to_owned();
        // SAFETY: The value has not been freed yet.
        unsafe { inkhole_string_free(value) };
        output
    }

    unsafe fn create_service() -> *mut InkholeService {
        let mut service = ptr::null_mut();
        let mut message = ptr::null_mut();
        // SAFETY: Both outputs are valid local pointers.
        let status = unsafe { inkhole_service_create(&mut service, &mut message) };
        assert_eq!(status, INKHOLE_STATUS_OK);
        assert!(message.is_null());
        assert!(!service.is_null());
        service
    }

    #[test]
    fn calls_json_and_validates_utf8_and_timeouts() {
        // SAFETY: This test retains exclusive ownership until destroy.
        let service = unsafe { create_service() };
        let request = br#"{"id":"ping-1","method":"ping","params":{}}"#;
        let mut output = ptr::null_mut();
        // SAFETY: All pointers and lengths are valid.
        let status =
            unsafe { inkhole_service_call(service, request.as_ptr(), request.len(), &mut output) };
        assert_eq!(status, INKHOLE_STATUS_OK);
        // SAFETY: output came from the FFI call.
        let response: Value = serde_json::from_str(&unsafe { take_output(output) }).unwrap();
        assert_eq!(response["id"], "ping-1");
        assert_eq!(response["ok"], true);

        let invalid = [0xff_u8];
        let mut output = ptr::null_mut();
        // SAFETY: All pointers and lengths are valid.
        let status =
            unsafe { inkhole_service_call(service, invalid.as_ptr(), invalid.len(), &mut output) };
        assert_eq!(status, INKHOLE_STATUS_INVALID_UTF8);
        // SAFETY: output came from the FFI call.
        assert!(unsafe { take_output(output) }.contains("UTF-8"));

        let mut output = ptr::null_mut();
        // SAFETY: All pointers are valid.
        let status = unsafe { inkhole_service_poll_event(service, -2, &mut output) };
        assert_eq!(status, INKHOLE_STATUS_INVALID_ARGUMENT);
        // SAFETY: output came from the FFI call.
        assert!(unsafe { take_output(output) }.contains("timeout_ms"));

        let mut output = ptr::null_mut();
        // SAFETY: All pointers are valid.
        let status = unsafe { inkhole_service_poll_event(service, 0, &mut output) };
        assert_eq!(status, INKHOLE_STATUS_NO_EVENT);
        assert!(output.is_null());

        let mut output = ptr::null_mut();
        // SAFETY: No other call is active and ownership is returned to Rust.
        let status = unsafe { inkhole_service_destroy(service, &mut output) };
        assert_eq!(status, INKHOLE_STATUS_OK);
        assert!(output.is_null());
    }

    #[test]
    fn one_runtime_supports_concurrent_calls_and_close_wakes_pollers() {
        // SAFETY: The test coordinates lifetime so destroy happens after joins.
        let service = unsafe { create_service() };
        let address = service as usize;
        let workers = (0..8)
            .map(|worker| {
                thread::spawn(move || {
                    let service = address as *mut InkholeService;
                    for call_index in 0..20 {
                        let request = format!(
                            r#"{{"id":"{worker}-{call_index}","method":"ping","params":{{}}}}"#
                        );
                        let mut output = ptr::null_mut();
                        // SAFETY: The handle remains live until every worker joins.
                        let status = unsafe {
                            inkhole_service_call(
                                service,
                                request.as_ptr(),
                                request.len(),
                                &mut output,
                            )
                        };
                        assert_eq!(status, INKHOLE_STATUS_OK);
                        // SAFETY: output came from the FFI call.
                        let response: Value =
                            serde_json::from_str(&unsafe { take_output(output) }).unwrap();
                        assert_eq!(response["ok"], true);
                    }
                })
            })
            .collect::<Vec<_>>();
        for worker in workers {
            worker.join().unwrap();
        }

        let barrier = Arc::new(Barrier::new(2));
        let poll_barrier = barrier.clone();
        let poller = thread::spawn(move || {
            let service = address as *mut InkholeService;
            poll_barrier.wait();
            let mut output = ptr::null_mut();
            // SAFETY: The handle remains live until this poller joins.
            let status = unsafe { inkhole_service_poll_event(service, -1, &mut output) };
            assert_eq!(status, INKHOLE_STATUS_CLOSED);
            // SAFETY: output came from the FFI call.
            assert!(unsafe { take_output(output) }.contains("closed"));
        });
        barrier.wait();
        thread::sleep(Duration::from_millis(20));
        let mut output = ptr::null_mut();
        // SAFETY: The service stays allocated while close wakes the poller.
        let status = unsafe { inkhole_service_close(service, &mut output) };
        assert_eq!(status, INKHOLE_STATUS_OK);
        assert!(output.is_null());
        poller.join().unwrap();

        let request = br#"{"id":"closed","method":"ping","params":{}}"#;
        let mut output = ptr::null_mut();
        // SAFETY: The handle is still allocated but closed.
        let status =
            unsafe { inkhole_service_call(service, request.as_ptr(), request.len(), &mut output) };
        assert_eq!(status, INKHOLE_STATUS_CLOSED);
        // SAFETY: output came from the FFI call.
        assert!(unsafe { take_output(output) }.contains("closed"));

        let mut output = ptr::null_mut();
        // SAFETY: All concurrent calls have completed.
        let status = unsafe { inkhole_service_destroy(service, &mut output) };
        assert_eq!(status, INKHOLE_STATUS_OK);
        assert!(output.is_null());
    }

    #[test]
    fn ffi_boundary_converts_panics_to_status_codes() {
        let mut output = ptr::null_mut();
        let status = ffi_boundary(&mut output, || panic!("ffi test panic"));
        assert_eq!(status, INKHOLE_STATUS_PANIC);
        // SAFETY: output came from ffi_boundary.
        assert!(unsafe { take_output(output) }.contains("ffi test panic"));
    }
}
