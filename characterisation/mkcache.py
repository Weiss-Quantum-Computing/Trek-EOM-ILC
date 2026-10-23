import numpy as np, os, json, pandas as pd
# the cache (and every reader's 'cache/index.json') lives beside these
# scripts, wherever this is started from (review G1-38)
os.chdir(os.path.dirname(os.path.abspath(__file__)))
from eomlib import files, ROOT
os.makedirs('cache', exist_ok=True)
idx = {}
for p in files():
    b = os.path.basename(p)[:-4]
    out = os.path.join('cache', b.replace(' ','_')+'.npy')
    if not os.path.exists(out):
        df = pd.read_csv(p)
        np.save(out, df.to_numpy(dtype=np.float64))
        hdr = list(df.columns)
    else:
        hdr = pd.read_csv(p, nrows=0).columns.tolist()
    idx[b] = dict(npy=out, hdr=hdr)
    print('ok', b, os.path.getsize(out)//1024, 'KB')
json.dump(idx, open('cache/index.json','w'), indent=1)
