import numpy as np, os, re, glob

# The 40 raw captures (~260 MB) are deliberately not in this repository.
# Point EOM_RAMPS_DIR at wherever they live before running any of these.
ROOT = os.environ.get(
    "EOM_RAMPS_DIR",
    os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))),          # the folder holding the repos
        "scope_data", "EOM ramps day 3"))

def load(path):
    with open(path) as f:
        hdr = f.readline().strip().split(',')
    d = np.loadtxt(path, delimiter=',', skiprows=1)
    return hdr, d

def meta(path):
    txt = path[:-4] + '.txt'
    m = {}
    if os.path.exists(txt):
        for line in open(txt):
            if ':' in line:
                k, v = line.split(':', 1)
                m[k.strip()] = v.strip()
    return m

def vdiv(m, ch):
    k = f'CH{ch} V/div'
    return float(m[k]) if k in m else None

def files():
    return sorted(glob.glob(os.path.join(ROOT, '*.csv')))

AMP_RE = re.compile(r'(?:conditioning|monitor)\s+([\d.]+)(?:\s+and\s+([\d.]+))?V')
def amp_of(name):
    mm = AMP_RE.search(name)
    if not mm: return None
    return (float(mm.group(1)), float(mm.group(2)) if mm.group(2) else float(mm.group(1)))
