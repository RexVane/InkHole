#ifndef INKHOLE_H
#define INKHOLE_H

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define INKHOLE_FFI_ABI_VERSION 1u

#define INKHOLE_STATUS_OK 0
#define INKHOLE_STATUS_NO_EVENT 1
#define INKHOLE_STATUS_INVALID_ARGUMENT 2
#define INKHOLE_STATUS_INVALID_UTF8 3
#define INKHOLE_STATUS_CLOSED 4
#define INKHOLE_STATUS_INTERNAL_ERROR 5
#define INKHOLE_STATUS_PANIC 6
#define INKHOLE_STATUS_WRONG_THREAD 7

typedef struct InkholeService InkholeService;

uint32_t inkhole_ffi_abi_version(void);

int32_t inkhole_service_create(
    InkholeService **out_service,
    char **out_message);

int32_t inkhole_service_call(
    InkholeService *service,
    const uint8_t *request,
    size_t request_len,
    char **out_json);

/* timeout_ms: -1 waits forever, 0 does not block, positive values are milliseconds. */
int32_t inkhole_service_poll_event(
    InkholeService *service,
    int64_t timeout_ms,
    char **out_event);

int32_t inkhole_service_reset(
    InkholeService *service,
    char **out_message);

int32_t inkhole_service_close(
    InkholeService *service,
    char **out_message);

/* No call may use service concurrently with destroy. The pointer is always consumed. */
int32_t inkhole_service_destroy(
    InkholeService *service,
    char **out_message);

/* Frees any non-null string returned through an out parameter. */
void inkhole_string_free(char *value);

#ifdef __cplusplus
}
#endif

#endif
