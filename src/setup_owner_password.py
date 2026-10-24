"""First unpacked start: choose a local password; no personal verifier is shipped."""
from pathlib import Path
import getpass, json, os, sys
APP=Path(__file__).resolve().parent
sys.path.insert(0,str(APP))
from cafe_auth import make_auth_policy

def configure(folder=APP, read_password=None, interactive=None):
    config=Path(folder)/'auth_config.json'
    if config.exists():
        print('Existing local password configuration retained.')
        return 0
    if interactive is None: interactive=sys.stdin.isatty()
    if not interactive:
        print('Open this script in a local interactive terminal to choose a password.')
        return 2
    read_password=read_password or getpass.getpass
    password=read_password('Choose local workbench password (8-128 chars): ')
    repeated=read_password('Confirm password: ')
    if password!=repeated or not 8<=len(password)<=128:
        print('Passwords must match and contain 8-128 characters. Nothing was saved.')
        return 2
    policy=make_auth_policy(password)
    temporary=config.with_name('auth_config.setup.tmp')
    temporary.write_text(json.dumps(policy,indent=2)+'\n',encoding='utf-8')
    os.replace(temporary,config)
    print('Local password saved as a salted verifier. No password is displayed.')
    return 0

if __name__=='__main__':
    raise SystemExit(configure())
