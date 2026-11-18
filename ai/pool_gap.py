"""単勝オッズと3連単オッズの食い違い（売上の少ない単勝が割安になっていないか）の検証。 python -m ai.pool_gap"""
import sys,collections,math
sys.path.insert(0,'.')
from pathlib import Path
from ai.model import load, market_win
rs=load(Path(sys.argv[1] if len(sys.argv)>1 else 'data'))
def run(rs,th,lo=0,hi=1e9):
    n=h=ret=0
    for r in rs:
        win=market_win(r); tns=r['odds'].get('単勝') or {}
        o=[x for x in r['result'] if x['order']==1]
        if not o: continue
        w1=o[0]['car']
        for c,p in win.items():
            od=tns.get(str(c)) or 0
            if od and lo<=od<hi and p*od>th:
                n+=1
                if c==w1: h+=1; ret+=od
    return n,h,ret/n*100 if n else 0
print('単勝オッズ × 3連単から出した1着確率 > 閾値 を買う（試走などは使わない）')
for th in (1.0,1.1,1.2,1.3):
    print(f' >{th}', run(rs,th))
print('\n年別 (>1.2)')
for y in ('2022','2023','2024','2025','2026'):
    print(' ',y, run([r for r in rs if r['date'][:4]==y],1.2))
print('\n場別 (>1.2)')
for p in ('川口','伊勢崎','浜松','飯塚','山陽'):
    print(' ',p, run([r for r in rs if r['place']==p],1.2))
print('\n単勝オッズ帯別 (>1.2)')
for lo,hi in ((1,3),(3,6),(6,10),(10,20),(20,1e9)):
    print(f'  {lo}-{hi}', run(rs,1.2,lo,hi))
# 比較: 単勝を全部買う
n=h=ret=0
for r in rs:
    tns=r['odds'].get('単勝') or {}; o=[x for x in r['result'] if x['order']==1]
    if not o: continue
    for c,od in tns.items():
        n+=1
        if int(c)==o[0]['car']: ret+=od
print('\n単勝全部買い 回収', round(ret/n*100,1))
