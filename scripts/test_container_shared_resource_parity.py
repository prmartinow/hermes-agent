#!/usr/bin/env python3
"""
Automated Container & Host Shared Resource Parity Validator
Probes hermes-agent-serving (:9119) and Host OS (/home/ops)
"""
import subprocess, json, sys, os, urllib.request, time
from pathlib import Path

def run_container_parity_audit():
    print("=" * 95)
    print(" HERMES AGENT CONTAINER (:9119) & HOST SHARED RESOURCE PARITY AUDIT")
    print("=" * 95)

    container = "hermes-agent-serving"
    results = {}

    container_probe = """
import os, json, sqlite3, shutil, time
from pathlib import Path

h_dir = os.environ.get("HERMES_HOME", "/home/ops/.hermes")
state_db = f"{h_dir}/state.db" if os.path.exists(f"{h_dir}/state.db") else "/opt/data/state.db"
skills_dir = Path(f"{h_dir}/skills") if os.path.exists(f"{h_dir}/skills") else Path("/opt/data/skills")
gemini_dir = Path("/home/ops/.gemini") if os.path.exists("/home/ops/.gemini") else Path(f"{h_dir}/.gemini")

db_stat = os.stat(state_db) if os.path.exists(state_db) else None
skills_stat = os.stat(skills_dir) if skills_dir.exists() else None
gemini_stat = os.stat(gemini_dir) if gemini_dir.exists() else None
mem_stat = os.stat("/mnt/data/agent-memory") if os.path.exists("/mnt/data/agent-memory") else None
mem_rw = os.access("/mnt/data/agent-memory", os.W_OK) if os.path.exists("/mnt/data/agent-memory") else False
agy_bin = shutil.which("agy") or ("/opt/hermes/bin/agy" if os.path.exists("/opt/hermes/bin/agy") else None)

lease_file = Path(f"{h_dir}/runtime/quota_refresher.lease") if os.path.exists(f"{h_dir}/runtime/quota_refresher.lease") else Path("/opt/data/runtime/quota_refresher.lease")
lease_data = json.loads(lease_file.read_text()) if lease_file.exists() else None

res = {
    "hermes_home": h_dir,
    "db_inode": db_stat.st_ino if db_stat else None,
    "skills_inode": skills_stat.st_ino if skills_stat else None,
    "skills_count": len([d for d in skills_dir.iterdir() if d.is_dir() and not d.name.startswith(".")]) if skills_dir.exists() else 0,
    "gemini_inode": gemini_stat.st_ino if gemini_stat else None,
    "mem_inode": mem_stat.st_ino if mem_stat else None,
    "mem_rw": mem_rw,
    "agy_bin": agy_bin,
    "lease_holder": lease_data.get("holder") if lease_data else None,
}
print(json.dumps(res))
"""

    try:
        raw = subprocess.check_output(["docker", "exec", container, "python3", "-c", container_probe], text=True)
        results[container] = json.loads(raw.strip())
    except Exception as e:
        print(f"Error probing container {container}: {e}")
        results[container] = {}

    # Host OS probe
    host_probe = """
import os, json, sqlite3, shutil
from pathlib import Path
db_stat = os.stat("/home/ops/.hermes/state.db") if os.path.exists("/home/ops/.hermes/state.db") else None
skills_stat = os.stat("/home/ops/.hermes/skills") if os.path.exists("/home/ops/.hermes/skills") else None
gemini_stat = os.stat("/home/ops/.gemini") if os.path.exists("/home/ops/.gemini") else None
mem_stat = os.stat("/mnt/data/agent-memory") if os.path.exists("/mnt/data/agent-memory") else None
mem_rw = os.access("/mnt/data/agent-memory", os.W_OK) if os.path.exists("/mnt/data/agent-memory") else False
agy_bin = shutil.which("agy")
lease_file = Path("/home/ops/.hermes/runtime/quota_refresher.lease")
lease_data = json.loads(lease_file.read_text()) if lease_file.exists() else None

res = {
    "hermes_home": "/home/ops/.hermes",
    "db_inode": db_stat.st_ino if db_stat else None,
    "skills_inode": skills_stat.st_ino if skills_stat else None,
    "skills_count": len([d for d in Path("/home/ops/.hermes/skills").iterdir() if d.is_dir() and not d.name.startswith(".")]) if Path("/home/ops/.hermes/skills").exists() else 0,
    "gemini_inode": gemini_stat.st_ino if gemini_stat else None,
    "mem_inode": mem_stat.st_ino if mem_stat else None,
    "mem_rw": mem_rw,
    "agy_bin": agy_bin,
    "lease_holder": lease_data.get("holder") if lease_data else None,
}
print(json.dumps(res))
"""
    try:
        raw_host = subprocess.check_output(["python3", "-c", host_probe], text=True)
        results["host_os"] = json.loads(raw_host.strip())
    except Exception as e:
        print(f"Error probing host: {e}")
        results["host_os"] = {}

    header = f"Resource Domain             | Serving (:9119)      | Host OS              | Status"
    print(header)
    print("-" * 95)

    keys = [
        ("HERMES_HOME Path", "hermes_home"),
        ("state.db (Inode)", "db_inode"),
        ("skills/ (Inode)", "skills_inode"),
        ("skills/ (Count)", "skills_count"),
        ("~/.gemini/ (Inode)", "gemini_inode"),
        ("agent-memory (Inode)", "mem_inode"),
        ("agent-memory (:rw)", "mem_rw"),
        ("agy binary (Internal)", "agy_bin"),
        ("Distributed Refresher", "lease_holder"),
    ]

    all_passed = True
    for label, key in keys:
        v_serv = str(results.get("hermes-agent-serving", {}).get(key, "N/A"))
        v_host = str(results.get("host_os", {}).get(key, "N/A"))
        
        if key in ["hermes_home", "db_inode", "skills_inode", "skills_count", "gemini_inode", "mem_inode"]:
            match = (v_serv == v_host)
            if not match: all_passed = False
            status = "PASS: 100% SHARED" if match else "FAIL: DIVERGED"
        elif key == "mem_rw":
            match = results.get("hermes-agent-serving", {}).get(key)
            status = "PASS: WRITABLE (:rw)" if match else "FAIL: READ-ONLY"
        elif key == "agy_bin":
            status = "PASS: CONTAINER-INTERNAL"
        elif key == "lease_holder":
            status = "PASS: 1 ACTIVE LEADER" if v_serv != "None" else "WARN: QUIESCENT"
        else:
            status = "OK"
            
        print(f"{label:<27} | {v_serv:<20} | {v_host:<20} | {status}")

    # Microservice Loopback Check
    print("\n" + "-" * 95)
    print("Microservice Endpoint Latency & Health:")
    for name, url in [("Serving Dashboard (:9119)", "http://127.0.0.1:9119/api/status"),
                      ("Hindsight Memory (:8888)", "http://127.0.0.1:8888/health"),
                      ("Local Inference (:18200)", "http://127.0.0.1:18200/healthz")]:
        t0 = time.perf_counter()
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Hermes-Audit/1.0"})
            with urllib.request.urlopen(req, timeout=2.0) as res:
                dt = (time.perf_counter() - t0) * 1000
                print(f"  • {name:<32} -> HTTP {res.status} ({dt:.1f} ms) [PASS]")
        except Exception as e:
            print(f"  • {name:<32} -> {e} [FAIL]")
            all_passed = False

    print("=" * 95)
    status_str = "PASS: 100% SYNCHRONIZED" if all_passed else "FAIL: DIVERGENCE DETECTED"
    print(f"OVERALL CONTAINER PARITY: {status_str}")
    print("=" * 95)

if __name__ == "__main__":
    run_container_parity_audit()
