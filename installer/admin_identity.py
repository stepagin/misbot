"""Local choice of admin bot identity before its ordinary MAS account is created."""
import argparse
import json
import os
from pathlib import Path
import re

import configuration
import federation_runtime
import state_store


def choose(root, tag, administrator=None):
    root=Path(root)
    metadata=configuration.read_private_json(root/'installation.json')
    saved=federation_runtime.identity(root,metadata)
    mxid=tag if ':' in tag else '@'+tag.removeprefix('@')+':'+metadata['domain']
    pattern=r'@[a-z0-9][a-z0-9._=-]{0,63}:'+re.escape(metadata['domain'])
    if not re.fullmatch(pattern,mxid) or (administrator and not re.fullmatch(pattern,administrator)):
        raise ValueError('Use a local Matrix tag')
    created=metadata.get('federation_provisioned') or any((root/'data/referral-control'/name).exists()
        for name in ('federation-session.json','managed-federation-user.json'))
    if created and mxid!=saved['user_id']:
        raise RuntimeError('Тег созданного бота изменить нельзя. Укажите его сохранённый тег.')
    if mxid in ('@referral-bot:'+metadata['domain'],'@server-guide:'+metadata['domain']) or mxid==(administrator or saved['administrator_mxid']):
        raise ValueError('Bot identity must differ from other bots and the administrator')
    saved['user_id']=mxid
    if administrator:saved['administrator_mxid']=administrator
    state_store.write_json(root/'config/admin-panel-identity.json',saved)
    path=root/'config/federation-bot.json'
    if path.exists():
        config=json.loads(path.read_text())
        if not created:config['user_id']=mxid
        config['administrator_mxids']=[saved['administrator_mxid']]
        state_store.write_json(path,config)
    return saved


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('action',choices=('show','set'))
    parser.add_argument('--directory',type=Path,required=True)
    parser.add_argument('--tag')
    parser.add_argument('--administrator')
    args=parser.parse_args()
    state_store.require_root()
    __import__('command_wrapper').trusted_installation(args.directory)
    with state_store.operation_guard(args.directory):
        if args.action=='set':
            if not args.tag:raise ValueError('Specify --tag')
            result=choose(args.directory,args.tag,args.administrator)
        else:result=federation_runtime.identity(args.directory,configuration.read_private_json(args.directory/'installation.json'))
        print(json.dumps(result,ensure_ascii=False))


if __name__=='__main__':
    try:main()
    except (OSError,RuntimeError,ValueError):
        print('Identity не изменена. Тег локальный, уникальный и неизменяемый после создания; misbot admin bot show.')
        raise SystemExit(1)
