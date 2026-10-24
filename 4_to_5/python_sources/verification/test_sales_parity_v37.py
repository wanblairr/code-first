"""Compare local Python v3.7 sales math with the bundled public JavaScript.
No HTTP, credentials, provider calls or private runtime are used.
"""
from pathlib import Path
import argparse
from datetime import date, timedelta
import json
import math
import random
import subprocess
import sys


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--local',type=Path,default=Path(__file__).resolve().parents[1]/'local_app')
    parser.add_argument('--public',type=Path,default=Path(__file__).resolve().parents[1]/'public_website')
    parser.add_argument('--node',default='node')
    parser.add_argument('--report',type=Path)
    args=parser.parse_args()
    sys.path.insert(0,str(args.local.resolve()))
    from cafe_bom import load_bom_data
    from cafe_sales_learning import build_popularity,forecast_from_history,price_extra_purchases
    data=load_bom_data()
    rng=random.Random(370024)
    today=date(2026,10,24)
    context={'targetDate':'2026-10-25','forecastCups':172.8,'referenceCups':211.7,
        'signature':'parity-context','weather':{'temperatureC':27,'precipitationMm':24},'holiday':{'isHoliday':False}}
    variants=[{'productId':pid,'size':size} for pid,p in data['products'].items() for size in p['recipes']]
    fixtures=[]
    for count in (0,1,2,7,28,60,180,366):
        days=[]
        for index in range(count):
            rows=[]
            for variant in rng.sample(variants,rng.randrange(min(len(variants),12)+1)):
                rows.append(dict(variant,quantity=rng.randrange(150)))
            days.append({'date':(today-timedelta(days=1+index)).isoformat(),'rows':rows})
        history={'revision':count,'days':days}
        fixtures.append({'history':history,'context':context,'extras':[
            {'id':'custom_cups','name':'Paper cups / 纸杯','quantity':2,'unit':'box','unitPriceCents':1025},
            {'id':'custom_free','name':'Free sample','quantity':1,'unit':'pcs','unitPriceCents':0}]})
    fixtures.append({'history':{'revision':1,'days':[{'date':'2026-10-23','rows':[
        {'productId':'BV-013','size':'中杯','quantity':70},{'productId':'BV-003','size':'中杯','quantity':35}]}]},
        'context':context,'extras':[]})
    # No implicit expiry for old user records; weights are normalized to latest day.
    fixtures.append({'history':{'revision':2,'days':[{'date':'2000-01-01','rows':[]},
        {'date':'2000-01-08','rows':[{'productId':'BV-013','size':'中杯','quantity':70}]}]},'context':context,'extras':[]})
    py=[{'popularity':build_popularity(data,x['history'],today.isoformat()),
         'forecast':forecast_from_history(data,x['context'],x['history'],today.isoformat()),
         'extras':price_extra_purchases(data,x['extras'])} for x in fixtures]
    js="""import fs from 'node:fs';
const input=JSON.parse(fs.readFileSync(0,'utf8'));
const m=await import(input.module);
process.stdout.write(JSON.stringify(input.fixtures.map(x=>({
popularity:m.buildPopularity(input.data,x.history,{asOfDate:input.today}),
forecast:m.forecastFromHistory(input.data,x.context,x.history,{asOfDate:input.today}),
extras:m.priceExtraPurchases(input.data,x.extras)}))));"""
    payload={'module':(args.public.resolve()/'dist/demo/sales-learning.mjs').as_uri(),
        'data':data,'fixtures':fixtures,'today':today.isoformat()}
    result=subprocess.run([args.node,'--input-type=module','-e',js],input=json.dumps(payload,ensure_ascii=False),
        text=True,encoding='utf-8',capture_output=True,check=True)
    public=json.loads(result.stdout)
    checks=0
    def equal(left,right,path='root'):
        nonlocal checks
        if isinstance(left,(int,float)) and not isinstance(left,bool) and isinstance(right,(int,float)) and not isinstance(right,bool):
            assert math.isclose(left,right,rel_tol=1e-12,abs_tol=1e-10),path
        elif isinstance(left,dict):
            assert isinstance(right,dict) and left.keys()==right.keys(),path
            for key in left: equal(left[key],right[key],path+'.'+key)
        elif isinstance(left,list):
            assert isinstance(right,list) and len(left)==len(right),path
            for i,(a,b) in enumerate(zip(left,right)):equal(a,b,path+f'[{i}]')
        else: assert left==right,path
        checks+=1
    equal(py,public)
    report={'success':True,'fixture_count':len(fixtures),'value_checks':checks,
        'scope':'Python/public JS popularity, size forecasts, zero sales, 366 saved days, old history and custom integer-cent prices',
        'external_api_calls':0}
    if args.report:
        args.report.parent.mkdir(parents=True,exist_ok=True)
        args.report.write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(report))


if __name__=='__main__':main()
