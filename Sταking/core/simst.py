info = '''
simst - Sim Stake/Strat, version 0.0.1
Copyright (c) 2025 Mobius Fund
Author: Jake Fan, jake@mobius.fund
License: The MIT License

Usage:  simst strategies_csv [-f initial_fund] [-e end_date]
        simst -h

Options:
-f, --fund      initial fund that overrides the fund column in the csv file
-e, --end       end date that differs from yesterday (the default)
-h, --help      print this message and exit

Examples:
        simst ../strat/simst.csv
        simst /tmp/test.csv -f 5000 -e 2025-06-30

Notes: The strategies csv file has 5 columns: uid, date, block, fund, strat.
A strategy is uniquely identified by the 'uid' column. The 'block' column is
only effective for live mining and currently ignored for general usage. For
a given strategy, the 'fund' column is only effective once and ignored by
subsequent rebalancing. Rebalancing can only happen once for a given date,
at the first block after midnight UTC. Multiple allocations on the same date
are ignored. The lack of block-level precision is because block-by-block
market data is too resource demanding to be publically available.

This tool is a part of Sταking, the Bittensor subnet that optimizes staking
strategies. Please visit:

https://github.com/mobiusfund/staking
'''

import os, sys, time
import json, requests
import numpy as np
import pandas as pd
import sqlalchemy as sql
from .const import *

kk = ['uid', 'hotkey']
kb = [*kk, 'block']
kn = [*kk, 'netuid']
cd = os.path.dirname(os.path.realpath(__file__))

class SimSt():
    def __init__(self, st=pd.DataFrame()):
        self.st = st
        self.bn = self.fetchdb()
        self.rv = self.bn[:0]
        self.fi = self.initfund()
        self.ba = pd.DataFrame(columns=[*kk, 'date', 'netuid', 'block_close', 'alpha_close'])
        self.hl = pd.read_csv(f'{cd}/db/ochl.col')
        self.pl = pd.read_csv(f'{cd}/db/pnl.col')
        self.sc = pd.read_csv(f'{cd}/db/score.col')

def ddclean(dd):
    dd = dd.drop_duplicates(['block', 'ochl'], keep='last')
    dd = dd.sort_values('block').reset_index(drop=True)
    dd.iloc[:,5:-1] = dd.iloc[:,5:-1].interpolate().bfill().ffill()
    dd = dd.drop(dd[dd['ochl'] == 'o'].index[1:])
    dd = dd.drop(dd[dd['ochl'] == 'high'].sort_values('price').index[:-1])
    dd = dd.drop(dd[dd['ochl'] == 'low'].sort_values('price').index[1:])
    dd = dd.drop(dd[dd['ochl'] == 'c'].index[:-1])
    return dd

def fetchdb(self):
    st = self.st
    if not len(st): return pd.DataFrame()
    date = st['date'].min()
    db = f'{cd}/db/daily.db'
    conn = sql.create_engine(f'sqlite:///{db}').connect()
    if not os.path.getsize(db):
        pd.read_csv(f'{db[:-3]}.00').to_sql('bndaily', conn, index=False)
    bn = pd.read_sql('SELECT * FROM bndaily', conn)
    last = bn['date'].iat[-1] if len(bn) > 1 else FIRST_DATE
    if last >= time.strftime('%F', time.gmtime(time.time() - 86400)):
        return bn[bn['date'] >= date]
    print(f'Fetch DB, begin {last}...', end='', flush=True)
    try: r, e = json.loads(requests.get(f'{API_ROOT}/daily/{last}').json()), 'done'
    except: r, e = [], 'error'
    df = pd.DataFrame(r, None, bn.columns).astype(bn.dtypes)
    if len(bn) > 1: df = df[df['date'] > last]
    bn = pd.concat([bn, df])
    print(f" {e}, end {bn['date'].iat[-1] if len(bn) > 1 else last}.")
    df.to_sql('bndaily', conn, if_exists='append', index=False)
    conn.commit()
    return bn[bn['date'] >= date]

def initfund(self):
    st, bn = self.st, self.bn
    if not len(bn): return pd.DataFrame()
    notin = 'init' not in st
    fi = pd.DataFrame(columns=[*kk, 'date', 'block', 'init', 'fund', 'strat'])
    for _, dd in st.iterrows():
        date = max(bn['date'].iat[0], dd['date'])
        block = bn[bn['date'] >= date]['block'].iat[0] if notin else dd['block']
        init = int(dd['uid'] not in fi['uid'].values) if notin else dd['init']
        hk = '' if 'hotkey' not in st else dd['hotkey']
        fi.loc[len(fi)] = [dd['uid'], hk, date, block, init, *dd[['fund', 'strat']]]
    fi['strat'] = fi['strat'].str.replace(r'''[^{'\w":.,}]''', '', regex=True)
    self.rv = bn[bn['block'].isin(fi['block'].values)].copy()
    self.rv['ochl'] = 'rv'
    return fi.drop_duplicates(kb)

def pldaily(self, date):
    bn, rv, fi, ba = self.bn, self.rv, self.fi, self.ba
    dn = bn[bn['date'] == date]
    fa = pd.DataFrame(columns=[*kk, 'block', 'netuid', 'init', 'fund', 'alloc'])
    for _, dd in fi[fi['date'] == date].iterrows():
        try: strat = eval(dd['strat'])
        except: continue
        if sum(strat.values()) > 1: continue
        strat ={j:strat[j] for j in strat if j and j in dn['netuid'].values}
        strat[0] = 1 - sum(strat.values())
        for n in strat: fa.loc[len(fa)] = [*dd[kb], n, *dd[['init', 'fund']], strat[n]]
    bb = fa[kb].drop_duplicates().sort_values(kb)
    nn = pd.concat([ba[kn], fa[kn]]).drop_duplicates().sort_values(kn).reset_index()
    dn = pd.concat([dn, rv[rv['date'] == date]])
    ba, fa = ba.set_index(kn).sort_index(), fa.set_index(kb).sort_index()

    dg = pd.DataFrame()
    for i in nn.index:
        uid, hk, netuid = key = nn.loc[i,kn]
        blk = bb[(bb['uid'] == uid) & (bb['hotkey'] == hk)]['block'].values
        dd = ddclean(dn[dn['netuid'] == netuid])
        dd = dd[(dd['ochl'] != 'rv') | dd['block'].isin(blk)]
        if not len(dd): continue
        try: block0, alpha0 = ba.loc[key].iloc[-2:]
        except: block0, alpha0 = dd['block'].iat[0], 0
        blocks = dd['block'].diff()
        blocks.iat[0] = dd['block'].iat[0] - block0
        dd.loc[dd['weight'] == 0, 'weight'] = 1e18
        onediv = list(dd['emission'] * (1 - VALI_TAKE) * blocks / (dd['tempo'] + 1) / dd['weight'])
        alpha = [alpha0 * (1 + onediv[0])]
        for i in range(1, len(dd)): alpha.append(alpha[i-1] * (1 + onediv[i]))
        dd['onediv'] = onediv
        dd['alpha'] = alpha
        dd['value'] = dd['alpha'] * dd['price']
        dd['init'] = 0.0
        dd['swap'] = [float('nan')] * len(dd)
        dd.loc[dd['onediv'].isna(), 'onediv'] = 0
        dd.insert(0, 'hotkey', hk)
        dd.insert(0, 'uid', uid)
        dg = pd.concat([dg, dd])

    alpha0 = {}
    dh = pd.DataFrame()
    for gg, dd in dg.groupby(kb) if len(dg) else []:
        uid, hk, block = gg
        dd = dd.reset_index(drop=True)
        rev = 'rv' in dd['ochl'].values
        alpha0n = {}
        for netuid in dd['netuid'].values:
            key = uid, hk, netuid
            if key not in alpha0:
                try: alpha0[key] = ba.loc[key].iat[-1]
                except: alpha0[key] = 0
            alpha0n[netuid] = alpha0[key]
        dd['alpha'] = dd['netuid'].map(alpha0n.get) * (1 + dd['onediv'])
        dd['value'] = dd['alpha'] * dd['price']

        value = dd.drop_duplicates(kn)['value'].sum()
        for i in dd.index:
            di = dict(dd.loc[i])
            netuid = di['netuid']
            key = uid, hk, netuid
            if rev:
                try: init, fund, alloc = fa.loc[gg].set_index('netuid').loc[netuid]
                except: init, fund, alloc = 0, 0, 0
                if not init and fund and alloc: self.stupdate(key, value)
                diff = (init * fund or value) * alloc - di['value']
                di['alpha'] += di['alpha_in'] - di['tao_in'] * di['alpha_in'] / (di['tao_in'] + diff) if netuid else diff
                if di['alpha'] < 0 or di['alpha'] >= di['alpha_in']: di['alpha'] = 0
                dd.loc[i,'alpha'] = di['alpha']
                dd.loc[i,'value'] = di['alpha'] * di['price']
                dd.loc[i,'init'] = init * fund * alloc
            dd.loc[i,'swap'] = di['tao_in'] - di['tao_in'] * di['alpha_in'] / (di['alpha_in'] + di['alpha']) if netuid else di['alpha']
            alpha0[key] = di['alpha']
        dh = pd.concat([dh, dd])

    col = ['block', 'price', 'alpha', 'value', 'swap']
    hl = self.hl.copy()
    for gg, dd in dh.groupby(kn) if len(dh) else []:
        dd = dd.reset_index(drop=True)
        if dd['init'].any():
            dd.loc[dd['init'] > 0, 'value'] = dd.loc[dd['init'] > 0, 'init']
            dd = dd[dd['block'] >= dd[dd['init'] > 0]['block'].iat[0]]
        loc  = [dd.iloc[0][col]]
        loc += [dd[dd['value'] == dd['value'].max()].iloc[-1][col]]
        loc += [dd[dd['value'] == dd['value'].min()].iloc[-1][col]]
        loc += [dd.iloc[-1][col]]
        hl.loc[len(hl)] = [*gg[:2], date, gg[2]] + [a for z in zip(*loc) for a in z]

    pl = self.pl
    for gg, dd in dh.groupby(kk) if len(dh) else []:
        dd = dd.reset_index(drop=True)
        if dd['init'].any():
            dd.loc[dd['init'] > 0, 'value'] = dd.loc[dd['init'] > 0, 'init']
            dd = dd[dd['block'] >= dd[dd['init'] > 0]['block'].iat[0]]
        dd = dd[dd['ochl'].isin(['o', 'c', 'hour', 'rv'])].drop_duplicates(['block', 'netuid'])
        dd = dd[kb + col[-2:]].groupby(kb).sum().reset_index()
        loc  = [dd.iloc[0,-3:]]
        loc += [dd[dd['value'] == dd['value'].max()].iloc[-1,-3:]]
        loc += [dd[dd['value'] == dd['value'].min()].iloc[-1,-3:]]
        loc += [dd.iloc[-1,-3:]]
        pl.loc[len(pl)] = [*gg, date] + [a for z in zip(*loc) for a in z]

    self.ba = hl[self.ba.columns]
    self.dnappend(dh, date)
    self.hlappend(hl)
    self.plappend(pl)

def pl2sc(self):
    pl, sc = self.pl, self.sc
    pl = pl.sort_values([*pl.columns[:3]])
    for gg, dd in pl.groupby([*pl.columns[:2]]) if len(pl) else []:
        init = dd['value_open'].iat[0]
        dd['pnl'] = dd['value_close'].diff()
        dd['pnl%'] = dd['pnl'] / dd['value_close'].shift() * 100
        dd['pnl'].iat[0] = dd['value_close'].iat[0] - init
        dd['pnl%'].iat[0] = dd['pnl'].iat[0] / init * 100
        sc.loc[len(sc)] = [*gg, dd['date'].iat[-1], *score(dd)]
    self.sc = sc.sort_values(['score', 'apy%'], ascending=False)
    self.scappend(sc)

def sc2pct(self):
    sc = self.sc.copy()
    jj = [j for j in sc.columns if j[-1:] == '%' and j != 'daily%']
    sc[jj] = sc[jj].map('{:.2f}%'.format)
    sc['daily%'] = sc['daily%'].map('{:.4f}%'.format)
    if not sc['hotkey'].sum(): sc = sc.drop('hotkey', axis=1)
    return sc

def kelly(p, b): return (p * (b + 1) - 1) / b

def drawdown(pnl):
    peak, down = 0, 0
    gain = list(pnl.cumsum())
    for i in range(len(gain)):
        peak = max(peak, gain[i])
        down = max(down, peak - gain[i])
    return down

def score(dd):
    days = len(dd)
    prob = len(dd[dd['pnl%'] > 0]) / days
    pavg = dd['pnl%'][dd['pnl%'] > 0].mean()
    lavg = dd['pnl%'][dd['pnl%'] < 0].mean() * -1
    init = dd['value_open'].iat[0]
    fund = dd['value_close'].iat[-1]
    swap = dd['swap_close'].iat[-1]
    gain = (fund - init) / init * 100
    risk = drawdown(dd['pnl%'])
    daily = ((1 + gain / 100) ** (1 / days) - 1) * 100
    apy = ((1 + daily / 100) ** 365 - 1) * 100
    mar = gain / (risk or 1)
    lsr = dd['pnl'].sum() / (dd['pnl'].abs().sum() or 1e18)
    odds = 50 + kelly(prob, pavg / lavg) / 2 * 100
    if odds <= 0: odds = 0
    if np.isnan(odds): odds = prob * 100
    if not risk and prob: mar = days ** 0.5 / 5
    score = mar * lsr * odds * daily
    if score <= 0: score = 0
    return [days,
        float('{:.2f}'.format(fund)),
        float('{:.2f}'.format(swap)),
        float('{:.2f}'.format(score)),
        float('{:.2f}'.format(apy)),
        float('{:.4f}'.format(lsr)),
        float('{:.2f}'.format(mar)),
        float('{:.2f}'.format(risk)),
        float('{:.2f}'.format(odds)),
        float('{:.4f}'.format(daily)),
        float('{:.2f}'.format(gain)),
    ]

def args():
    if len(sys.argv) == 2 and sys.argv[1] in ['-h', '--help']: print(info), exit()
    if len(sys.argv) == 1: print('simst - Sim Stake/Strat\n')

    import argparse
    parser = argparse.ArgumentParser(
        prog = 'simst',
        usage = 'simst strategies_csv [-f initial_fund] [-e end_date]',
        add_help = False,
    )
    parser.add_argument('csv', metavar='strategies_csv')
    parser.add_argument('-f', '--fund', default=0, type=float)
    parser.add_argument('-e', '--end', default='')
    parser.add_argument('-h', '--help', action='store_true')

    try: a = parser.parse_args()
    except: print("Try 'simst -h' for more info", file=sys.stderr), exit(1)
    if not os.path.isfile(a.csv) or not os.access(a.csv, os.R_OK):
        print(f"simst: error: file '{a.csv}' does not exist or is not readable", file=sys.stderr)
        exit(1)
    return a.csv, a.fund, a.end

def main():
    csv, fund, end = args()
    sim = SimSt(pd.read_csv(csv))
    if fund: sim.fi['fund'] = fund
    if end: sim.bn = sim.bn[sim.bn['date'] <= end]
    for date in sorted(sim.bn['date'].unique()):
        sim.pldaily(date)
    sim.pl2sc()
    if not len(sim.sc): return
    print(sim.sc2pct().to_string(index=False))

# reserved for live api server
def stupdate(self, k, v): pass
def dnappend(self, dh, date): pass
def hlappend(self, hl): pass
def plappend(self, pl): pass
def scappend(self, sc): pass

SimSt.fetchdb = fetchdb
SimSt.initfund = initfund
SimSt.pldaily = pldaily
SimSt.pl2sc = pl2sc
SimSt.sc2pct = sc2pct
SimSt.stupdate = stupdate
SimSt.dnappend = dnappend
SimSt.hlappend = hlappend
SimSt.plappend = plappend
SimSt.scappend = scappend

if __name__ == "__main__": main()
