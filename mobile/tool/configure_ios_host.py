"""Apply the iOS privacy declarations required by InkHole LAN discovery."""

from pathlib import Path
import plistlib


def main() -> None:
    plist_path = Path(__file__).resolve().parents[1] / "ios" / "Runner" / "Info.plist"
    if not plist_path.exists():
        raise SystemExit(f"Flutter iOS host is missing: {plist_path}")

    with plist_path.open("rb") as stream:
        plist = plistlib.load(stream)

    plist["NSLocalNetworkUsageDescription"] = (
        "InkHole uses the local network to discover nearby devices and transfer files."
    )
    services = set(plist.get("NSBonjourServices", []))
    services.add("_inkhole._udp")
    plist["NSBonjourServices"] = sorted(services)

    with plist_path.open("wb") as stream:
        plistlib.dump(plist, stream, sort_keys=False)


if __name__ == "__main__":
    main()
