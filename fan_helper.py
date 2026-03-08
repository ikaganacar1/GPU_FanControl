#!/usr/bin/env python3
"""Fan control helper - runs as root, receives commands via stdin.

Protocol (one JSON per line):
  {"cmd": "set", "gpu": 0, "fan": 0, "speed": 60}
  {"cmd": "reset", "gpu": 0, "fan": 0}
  {"cmd": "reset_all"}
  {"cmd": "quit"}

Responses (one JSON per line):
  {"ok": true}
  {"ok": false, "error": "message"}
"""

import sys
import json
import pynvml


def main():
    pynvml.nvmlInit()

    handles = {}
    count = pynvml.nvmlDeviceGetCount()
    for i in range(count):
        handles[i] = pynvml.nvmlDeviceGetHandleByIndex(i)

    # Track which fans we've taken control of, so we can reset on exit
    controlled_fans = set()  # (gpu_idx, fan_idx)

    def respond(ok, error=None):
        msg = {"ok": ok}
        if error:
            msg["error"] = error
        sys.stdout.write(json.dumps(msg) + "\n")
        sys.stdout.flush()

    def reset_all():
        for gpu_idx, fan_idx in controlled_fans:
            try:
                pynvml.nvmlDeviceSetDefaultFanSpeed_v2(handles[gpu_idx], fan_idx)
            except Exception:
                pass
        controlled_fans.clear()

    try:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                cmd = json.loads(line)
            except json.JSONDecodeError:
                respond(False, "invalid json")
                continue

            action = cmd.get("cmd")

            if action == "quit":
                reset_all()
                respond(True)
                break

            elif action == "set":
                gpu = cmd.get("gpu", 0)
                fan = cmd.get("fan", 0)
                speed = max(0, min(100, cmd.get("speed", 50)))
                if gpu not in handles:
                    respond(False, f"gpu {gpu} not found")
                    continue
                try:
                    pynvml.nvmlDeviceSetFanSpeed_v2(handles[gpu], fan, speed)
                    controlled_fans.add((gpu, fan))
                    respond(True)
                except Exception as e:
                    respond(False, str(e))

            elif action == "reset":
                gpu = cmd.get("gpu", 0)
                fan = cmd.get("fan", 0)
                if gpu not in handles:
                    respond(False, f"gpu {gpu} not found")
                    continue
                try:
                    pynvml.nvmlDeviceSetDefaultFanSpeed_v2(handles[gpu], fan)
                    controlled_fans.discard((gpu, fan))
                    respond(True)
                except Exception as e:
                    respond(False, str(e))

            elif action == "reset_all":
                reset_all()
                respond(True)

            else:
                respond(False, f"unknown cmd: {action}")

    except (KeyboardInterrupt, BrokenPipeError):
        pass
    finally:
        reset_all()
        pynvml.nvmlShutdown()


if __name__ == "__main__":
    main()
