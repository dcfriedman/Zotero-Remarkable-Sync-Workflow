"""
Checks that only one copy of the watcher can run per folder (and that the lock is
released when a copy ends or is killed).

Run:  python tests/test_single_instance.py
"""
import sys, types, importlib.util, subprocess, time
from pathlib import Path
SCRIPT = str(Path(__file__).resolve().parents[1] / "zotero_remarkable_sync.py")
sys.modules.setdefault("pyzotero", types.ModuleType("pyzotero")); sys.modules["pyzotero"].zotero = types.SimpleNamespace(Zotero=None)
spec = importlib.util.spec_from_file_location("zrs", SCRIPT)
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
a = m.acquire_single_instance_lock(); assert a is not None
# a second, separate process must be refused while we hold it
code = ("import sys,types,importlib.util;sys.modules['pyzotero']=types.ModuleType('pyzotero');sys.modules['pyzotero'].zotero=None;"
        "s=importlib.util.spec_from_file_location('z'," + repr(SCRIPT) + ");"
        "m=importlib.util.module_from_spec(s);s.loader.exec_module(m);print('GOT' if m.acquire_single_instance_lock() else 'REFUSED')")
r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True); assert r.stdout.strip() == "REFUSED", r
a.close()   # like the process ending
r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True); assert r.stdout.strip() == "GOT", r
# a killed holder releases it too
p = subprocess.Popen([sys.executable, "-c", code.replace("print('GOT' if m.acquire_single_instance_lock() else 'REFUSED')", "l=m.acquire_single_instance_lock();import time;time.sleep(60)")])
time.sleep(1.5)
r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True); assert r.stdout.strip() == "REFUSED", r
p.kill(); p.wait()
r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True); assert r.stdout.strip() == "GOT", r
print("LOCK TESTS PASSED")
