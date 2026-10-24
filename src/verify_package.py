"""Verify a freshly unpacked release. No network, credentials or runtime writes."""
from pathlib import Path, PurePosixPath
import hashlib, json, sys
ROOT=Path(__file__).resolve().parent

def main():
    manifest=json.loads((ROOT/'FINAL_FILES.json').read_text(encoding='utf-8'))
    errors=[]
    for item in manifest['files']:
        relative=PurePosixPath(item['path'])
        if relative.is_absolute() or '..' in relative.parts or ':' in relative.parts[0]:
            errors.append('Invalid path');continue
        source=ROOT.joinpath(*relative.parts)
        if source.is_symlink() or not source.is_file():
            errors.append(item['path']+': missing or symlink');continue
        data=source.read_bytes()
        if len(data)!=item['bytes'] or hashlib.sha256(data).hexdigest()!=item['sha256']:
            errors.append(item['path']+': changed')
    if errors:
        print('\n'.join(errors[:20]));return 2
    print(json.dumps({'success':True,'verified_files':len(manifest['files']),'network_calls':0,'credentials_read':False}))
    return 0
if __name__=='__main__':raise SystemExit(main())
