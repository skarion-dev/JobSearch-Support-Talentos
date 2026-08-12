"""
Inspect and manage gateway clients without going through the HTTP /admin API.
Reads/writes gateway/gateway.db directly, same as gateway_issue_key.py.

    python -m scripts.gateway_admin list
    python -m scripts.gateway_admin keys
    python -m scripts.gateway_admin stats --hours 24
    python -m scripts.gateway_admin disable --id 2
    python -m scripts.gateway_admin enable  --id 2
    python -m scripts.gateway_admin revoke  --id 2
    python -m scripts.gateway_admin kill-switch --off      # stop ALL traffic
    python -m scripts.gateway_admin kill-switch --on
"""
import argparse
import json

from gateway import store
from gateway.keys import pool


def cmd_list():
    for c in store.list_clients():
        models = json.loads(c["allowed_models"])
        state = "enabled" if c["enabled"] else ("revoked" if c["revoked_at"] else "disabled")
        print(f"  #{c['id']:<3} {c['name']:<20} {state:<10} "
              f"rpm={c['rpm_limit']:<5} daily_tokens={c['daily_token_limit']:<10} "
              f"models={models} token={c['token_prefix']}...")


def cmd_keys():
    for k in pool.status():
        flag = "DEAD" if k["dead"] else ("cooling" if k["cooling_down"] else "ok")
        print(f"  {k['prefix']:<14} {flag:<8} "
              f"{('reason=' + str(k['dead_reason'])) if k['dead'] else ''}"
              f"{('cooldown=' + str(k['cooldown_seconds_left']) + 's') if k['cooling_down'] else ''}")


def cmd_stats(hours: int):
    rows = store.usage_stats(hours)
    if not rows:
        print(f"No requests in the last {hours}h.")
        return
    for r in rows:
        print(f"  {r['name']:<20} {r['model']:<18} n={r['n']:<5} ok={r['ok']:<5} "
              f"errors={r['errors']:<4} rejected={r['rejected']:<4} "
              f"tokens={r['tokens']:<10} avg_latency_ms={r['avg_latency_ms']}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    sub.add_parser("keys")
    p_stats = sub.add_parser("stats")
    p_stats.add_argument("--hours", type=int, default=24)
    p_en = sub.add_parser("enable")
    p_en.add_argument("--id", type=int, required=True)
    p_dis = sub.add_parser("disable")
    p_dis.add_argument("--id", type=int, required=True)
    p_rev = sub.add_parser("revoke")
    p_rev.add_argument("--id", type=int, required=True)
    p_kill = sub.add_parser("kill-switch")
    g = p_kill.add_mutually_exclusive_group(required=True)
    g.add_argument("--on", action="store_true")
    g.add_argument("--off", action="store_true")
    a = ap.parse_args()

    if a.cmd == "list":
        cmd_list()
    elif a.cmd == "keys":
        cmd_keys()
    elif a.cmd == "stats":
        cmd_stats(a.hours)
    elif a.cmd == "enable":
        print("ok" if store.set_client_enabled(a.id, True) else "no such client")
    elif a.cmd == "disable":
        print("ok" if store.set_client_enabled(a.id, False) else "no such client")
    elif a.cmd == "revoke":
        print("ok" if store.revoke_client(a.id) else "no such client")
    elif a.cmd == "kill-switch":
        store.set_global_enabled(a.on)
        print(f"global_enabled = {a.on}")
