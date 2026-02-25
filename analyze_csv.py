# -*- coding: utf-8 -*-
"""CSV graph analysis - 구간/모양별 승률"""
import csv
from collections import defaultdict

path = r'c:\Users\pyo08\Downloads\graph_analysis_2026-02-25 (6).csv'
with open(path, 'r', encoding='utf-8-sig') as f:
    lines = f.readlines()
# Skip comment lines, find header (회차,픽,...)
start = 0
for i, line in enumerate(lines):
    if line.strip().startswith('회차') or (',' in line and '픽' in line and '실제' in line):
        start = i
        break
import io
reader = csv.DictReader(io.StringIO(''.join(lines[start:])), fieldnames=None)
rows = list(reader)

cols = list(rows[0].keys()) if rows else []
print('Cols:', cols[:12])
if not rows:
    print('No rows'); exit()
def get(r, key):
    return r.get(key, '')

# Find result column (승/패/조)
res_col = next((c for c in cols if '패' in c or c == '승패'), cols[3] if len(cols)>3 else '')
if not res_col:
    res_col = cols[3] if len(cols)>3 else cols[0]
rows = [r for r in rows if r.get(res_col, '').strip() in ('승','패')]
total = len(rows)
wins = sum(1 for r in rows if r.get(res_col, '').strip() == '승')
if total == 0:
    print('No rows with 승/패 found. Sample row keys:', list(rows[0].keys())[:8] if rows else 'empty')
else:
    print('Total:', wins, '/', total, '=', round(100*wins/total,1), '%')

by_gu = defaultdict(lambda: {'w':0,'l':0})
gu_col = next((c for c in cols if '구간' in c), '구간')
shape_col = next((c for c in cols if '모양' in c and '덩어리' in c), '덩어리모양')
pick_col = next((c for c in cols if c == '픽'), '픽')
act_col = next((c for c in cols if '실제' in c), '실제')
for r in rows:
    g = r.get(gu_col, '') or '-'
    if r.get(res_col)=='승': by_gu[g]['w']+=1
    else: by_gu[g]['l']+=1
print('Using cols: res=', res_col, 'gu=', gu_col, 'shape=', shape_col)
print('By section:')
for g in sorted(by_gu.keys(), key=lambda x: -(by_gu[x]['w']+by_gu[x]['l'])):
    w,l = by_gu[g]['w'], by_gu[g]['l']
    if w+l >= 25:
        print(' ', g, ':', w,'/',w+l,'=', round(100*w/(w+l),1), '%')

by_shape = defaultdict(lambda: {'w':0,'l':0})
for r in rows:
    s = r.get(shape_col, '') or '-'
    if r.get(res_col)=='승': by_shape[s]['w']+=1
    else: by_shape[s]['l']+=1
print('By shape:')
for s in sorted(by_shape.keys(), key=lambda x: -(by_shape[x]['w']+by_shape[x]['l'])):
    w,l = by_shape[s]['w'], by_shape[s]['l']
    if w+l >= 25:
        print(' ', s, ':', w,'/',w+l,'=', round(100*w/(w+l),1), '%')

jw = sum(1 for r in rows if r.get(pick_col)=='정' and r.get(act_col)=='정')
jl = sum(1 for r in rows if r.get(pick_col)=='정' and r.get(act_col)=='꺽')
kw = sum(1 for r in rows if r.get(pick_col)=='꺽' and r.get(act_col)=='꺽')
kl = sum(1 for r in rows if r.get(pick_col)=='꺽' and r.get(act_col)=='정')
print('Pick accuracy:')
if jw+jl: print('  정:', jw,'/',jw+jl,'=', round(100*jw/(jw+jl),1), '%')
if kw+kl: print('  꺽:', kw,'/',kw+kl,'=', round(100*kw/(kw+kl),1), '%')

# Section + shape combo (problem areas)
print()
print('=== Problem combos (section+shape, n>=15, win<48%)')
combo = defaultdict(lambda: {'w':0,'l':0})
for r in rows:
    g = r.get(gu_col, '') or '-'
    s = r.get(shape_col, '') or '-'
    key = g + '|' + s
    if r.get(res_col)=='승': combo[key]['w']+=1
    else: combo[key]['l']+=1
for k in sorted(combo.keys(), key=lambda x: -(combo[x]['w']+combo[x]['l'])):
    w,l = combo[k]['w'], combo[k]['l']
    pct = 100*w/(w+l) if (w+l) else 0
    if w+l >= 15 and pct < 48:
        print(' ', k, ':', w,'/',w+l,'=', round(pct,1), '%')

# 퐁당% 구간별
print()
print('=== By pong%% band')
pong_col = next((c for c in cols if '퐁당' in c and '%' in c), '퐁당%')
def pong_val(r):
    v = r.get(pong_col, '0')
    try: return float(str(v).replace(',','.').replace('%',''))
    except: return 50
band_high = [r for r in rows if pong_val(r) >= 65]
band_mid = [r for r in rows if 45 <= pong_val(r) < 65]
band_low = [r for r in rows if pong_val(r) < 45]
for name, arr in [('pong 65%+', band_high), ('pong 45-65%', band_mid), ('pong <45%', band_low)]:
    if arr:
        w = sum(1 for r in arr if r.get(res_col)=='승')
        print(' ', name, ':', w,'/',len(arr),'=', round(100*w/len(arr),1), '%')
