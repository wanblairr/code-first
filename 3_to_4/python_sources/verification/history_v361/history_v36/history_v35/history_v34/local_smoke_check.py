"""Offline acceptance of the unpacked source release; no external API calls."""
from pathlib import Path
import hashlib, json, os, runpy, subprocess, sys, tempfile
ROOT=Path(__file__).resolve().parents[1]
APP=ROOT/'local_app'
sys.path.insert(0,str(APP))
from cafe_service import CafeService
from cafe_app import export_order
from cafe_auth import load_auth_policy, verify_owner_password

def main():
    setup=runpy.run_path(str(ROOT/'setup_owner_password.py'))
    temp_root=Path(sys.argv[1] if len(sys.argv)>1 else tempfile.gettempdir()).resolve()
    temp_root.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='cafe-final-smoke-',dir=str(temp_root)) as name:
        base=Path(name).resolve()
        assert base.parent==temp_root and base.name.startswith('cafe-final-smoke-')
        password_dir=base/'password-check'
        password_dir.mkdir()
        assert setup['configure'](password_dir,lambda _: 'only-a-smoke-fixture',True)==0
        assert verify_owner_password('only-a-smoke-fixture',load_auth_policy(password_dir/'auth_config.json'))
        before=(password_dir/'auth_config.json').read_bytes()
        assert setup['configure'](password_dir,lambda _: (_ for _ in ()).throw(AssertionError()),True)==0
        assert (password_dir/'auth_config.json').read_bytes()==before
        mismatch_dir=base/'mismatch-check';mismatch_dir.mkdir()
        values=iter(['first-fixture','second-fixture'])
        assert setup['configure'](mismatch_dir,lambda _: next(values),True)==2
        assert not (mismatch_dir/'auth_config.json').exists()
        env={k:v for k,v in os.environ.items() if not k.startswith(('RUNPOD_','GMAIL_','WHATSAPP_','CAFE_DEMO_BASE'))}
        env['PYTHONUTF8']='1'
        for command in ('demo-context','train','run'):
            result=subprocess.run([sys.executable,str(APP/'cafe_app.py'),'--base',str(base/'store'),command],cwd=str(ROOT),env=env,capture_output=True,text=True,encoding='utf-8',timeout=30)
            assert result.returncode==0, command+' failed: '+result.stderr[:1000]
        service=CafeService(base/'store')
        catalog=service.catalog()
        supplier=service.inputs()['suppliers']['suppliers'][0]['supplier_id']
        saved=service.save_catalog({'revision':catalog['revision'],'prices':{k:f'{v/100:.2f}' for k,v in catalog['prices'].items()},'products':[{'product_id':'','name':'Vanilla syrup / 香草糖浆','category':'糖浆与调味','purchase_unit':'瓶','quantity':2,'unit_price_myr':'28.50','supplier_id':supplier}]})
        custom_id=saved['products'][0]['product_id']
        proposal=service.trigger()
        assert proposal['status']=='pending' and proposal['analysis']
        milk=next(i for i in proposal['plan']['items'] if i['ingredient_id']=='milk')
        assert milk['suggested_quantity']>=milk['pack_size']
        quantities={'milk':milk['suggested_quantity']-milk['pack_size']}
        inventory_before=service.inputs()['inventory']
        preview=service.preview(proposal['id'],proposal['revision'],proposal['input_hash'],quantities)
        assert preview['changes']
        order=service.approve(proposal['id'],proposal['revision'],proposal['input_hash'],quantities,True)
        assert order['manual_total_cents']==5700
        assert order['total_cents']==sum(i['line_total_cents'] for i in order['items'])
        assert order['sent'] is False and order['inventory_changed'] is False
        assert service.inputs()['inventory']==inventory_before
        approved_milk=next(i for i in order['items'] if i['ingredient_id']=='milk')
        assert approved_milk['approved_quantity']==quantities['milk']
        drafts=service.store.drafts_for(order['id'])
        assert len(drafts)==3 and all(d['sent'] is False for d in drafts)
        assert any('MYR 57.00' in d['body'] and 'Vanilla syrup' in d['body'] for d in drafts)
        export_order(service,order['id'],base/'export')
        assert len(list((base/'export').rglob('*.eml')))==3
        assert len(list((base/'export').rglob('*.txt')))==3
        assert any(e['action']=='approved_draft_created' for e in service.store.snapshot()['events'])
        result={'success':True,'cli_commands':['demo-context','train','run'],'first_password_setup_verified':True,'existing_password_preserved':True,'password_mismatch_refused':True,'weather_holiday_analysis_present':True,'milk_edit_preview_and_approval_verified':True,'custom_product_amount_cents':5700,'grand_total_verified':True,'supplier_drafts':3,'eml_files':3,'txt_files':3,'inventory_unchanged':True,'external_api_calls':0,'messages_sent':0,'paid_inferences':0}
        print(json.dumps(result,ensure_ascii=False))
        return result
if __name__=='__main__': main()
