"""Command line client: save/recall presets and verify them against the picture.

    export CAM_HOST=... CAM_USER=... CAM_PASS=...
    xiongmai-ptz slots
    xiongmai-ptz save 5 --label "Door"
    xiongmai-ptz goto 5
    xiongmai-ptz check 5 --mark          # snapshot the view, then compare later
    xiongmai-ptz clear 5

`goto` uses the backlash-compensated sequence by default; --plain sends a single
GotoPreset. `check` compares the current sub-stream frame with the saved one and
prints the offset in pixels: Ret 100 alone proves nothing.
"""

import argparse
import json
import os
import sys
from pathlib import Path

from .dvrip import Dvrip
from .ptz import check_slot, clear_preset, goto_preset, list_slots, save_preset


def camera(args):
    return Dvrip(args.host, args.user, args.password, port=args.port)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="xiongmai-ptz", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default=os.environ.get("CAM_HOST"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("CAM_PORT", 34567)))
    parser.add_argument("--user", default=os.environ.get("CAM_USER", "admin"))
    parser.add_argument("--password", default=os.environ.get("CAM_PASS"))
    parser.add_argument("--refs", default="refs", help="directory for reference frames")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("slots", help="list preset slots known to the camera")

    p_save = sub.add_parser("save", help="store the current position in a slot")
    p_save.add_argument("slot", type=int)
    p_save.add_argument("--label", default="")
    p_save.add_argument("--plain", action="store_true",
                        help="store as is, without loading the gears first")

    p_goto = sub.add_parser("goto", help="return to a slot")
    p_goto.add_argument("slot", type=int)
    p_goto.add_argument("--plain", action="store_true",
                        help="single GotoPreset, no backlash compensation")

    p_clear = sub.add_parser("clear", help="free a slot")
    p_clear.add_argument("slot", type=int)

    p_mark = sub.add_parser("mark", help="save a reference frame of this view")
    p_mark.add_argument("slot", type=int)
    p_mark.add_argument("--stream", type=int, default=1, help="0 = main, 1 = sub")

    p_check = sub.add_parser("check", help="compare the live view with the reference")
    p_check.add_argument("slot", type=int)
    p_check.add_argument("--stream", type=int, default=1, help="0 = main, 1 = sub")

    args = parser.parse_args(argv)
    if not args.host or not args.password:
        parser.error("set CAM_HOST and CAM_PASS (or pass --host/--password)")

    refs = Path(args.refs)
    refs.mkdir(parents=True, exist_ok=True)
    labels = refs / "labels.json"

    if args.command == "slots":
        with camera(args) as cam:
            print("slots:", list_slots(cam))
        return 0

    if args.command == "save":
        with camera(args) as cam:
            save_preset(cam, args.slot, approach=None if args.plain else "DirectionRightDown")
            slots = list_slots(cam)
        if args.label:
            data = json.loads(labels.read_text()) if labels.exists() else {}
            data[str(args.slot)] = args.label
            labels.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
        print(f"saved slot {args.slot}; slots now: {slots}")
        return 0

    if args.command == "goto":
        with camera(args) as cam:
            goto_preset(cam, args.slot, approach=None if args.plain else "DirectionRightDown")
        print(f"GotoPreset {args.slot} done "
              f"({'plain' if args.plain else 'with backlash compensation'})")
        return 0

    if args.command == "clear":
        with camera(args) as cam:
            clear_preset(cam, args.slot)
            slots = list_slots(cam)
        if labels.exists():
            data = json.loads(labels.read_text())
            data.pop(str(args.slot), None)
            labels.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
        for extra in (refs / f"slot{args.slot}.jpg", refs / f"slot{args.slot}-now.jpg"):
            extra.unlink(missing_ok=True)
        print(f"cleared slot {args.slot}; slots now: {slots}")
        return 0

    # mark / check need a picture, so PyAV and numpy are imported lazily
    from PIL import Image

    from .measure import grab_frame, rtsp_url, shift

    url = rtsp_url(args.host, args.user, args.password, stream=args.stream,
                   port=int(os.environ.get("CAM_RTSP_PORT", 554)))

    if args.command == "mark":
        frame = grab_frame(url)
        target = refs / f"slot{args.slot}.jpg"
        frame.save(target, quality=90)
        print(f"reference saved: {target} {frame.size}")
        return 0

    reference = refs / f"slot{args.slot}.jpg"
    if not reference.exists():
        sys.exit(f"no reference {reference}: run `mark {args.slot}` first")
    base = Image.open(reference).convert("RGB")
    frames = [grab_frame(url) for _ in range(3)]
    frames[-1].save(refs / f"slot{args.slot}-now.jpg", quality=90)
    offsets = [shift(base, frame) for frame in frames]
    best = min(offsets, key=lambda o: abs(o[0]) + abs(o[1]))
    print("offsets (dx, dy, peak):", offsets)
    print(f"best match: dx={best[0]} dy={best[1]} peak={best[2]}")
    if abs(best[0]) <= 6 and abs(best[1]) <= 6 and best[2] >= 0.1:
        print("verdict: same view (within a few pixels)")
    elif best[2] < 0.1:
        print("verdict: different view (weak correlation)")
    else:
        print("verdict: shifted view")
    return 0


if __name__ == "__main__":
    sys.exit(main())
