#!/usr/bin/env python3
"""Minimal NVIDIA NIM terminal agent for Alpine and other Unix-like systems."""
import argparse, getpass, json, os, subprocess, time, urllib.request, uuid

TOOL = {'type':'function','function':{'name':'terminal','description':'Execute shell commands on this host for the user request.','parameters':{'type':'object','properties':{'command':{'type':'string'},'cwd':{'type':'string'},'timeout':{'type':'integer'}},'required':['command']}}}

def api(base, key, path, data=None, method='POST'):
    raw = None if data is None else json.dumps(data).encode()
    req = urllib.request.Request(base.rstrip('/') + path, raw, method=method, headers={'Authorization':'Bearer '+key,'Content-Type':'application/json'})
    with urllib.request.urlopen(req, timeout=180) as response:
        return json.loads(response.read())

def is_rate_limit(error):
    return getattr(error, 'code', None) == 429 or 'rate limit' in str(error).lower() or 'too many requests' in str(error).lower()

def trace(enabled, message):
    if enabled: print('[trace] ' + message, flush=True)

def session_dir():
    path = os.path.expanduser(os.getenv('CKI_LITE_HOME','~/.cki-lite'))
    os.makedirs(path, exist_ok=True)
    return path

def save_session(session_id, model, history, started):
    with open(os.path.join(session_dir(), session_id+'.json'),'w',encoding='utf-8') as f:
        json.dump({'session_id':session_id,'started_at':started,'updated_at':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),'model':model,'messages':history},f,ensure_ascii=False,indent=2)

def load_session(session_id):
    path=os.path.join(session_dir(),session_id+'.json')
    if not os.path.exists(path): return None
    with open(path,encoding='utf-8') as f: return json.load(f)

def export_sessions(target, session_id=None):
    names=[session_id+'.json'] if session_id else sorted(x for x in os.listdir(session_dir()) if x.endswith('.json'))
    data=[]
    for name in names:
        item=load_session(name[:-5])
        if item: data.append(item)
    with open(target,'w',encoding='utf-8') as f: json.dump(data[0] if session_id and data else data,f,ensure_ascii=False,indent=2)
    print('Exportado: %s (%d sessão(ões))' % (target,len(data)))

def shell(args, verbose=False):
    command = args.get('command',''); cwd = args.get('cwd') or os.getcwd(); timeout = min(int(args.get('timeout',120)),900)
    started = time.time(); trace(verbose, 'terminal start cwd=%s timeout=%ss' % (cwd, timeout))
    try:
        p = subprocess.run(['/bin/sh','-lc',command], cwd=cwd, text=True, capture_output=True, timeout=timeout)
        trace(verbose, 'terminal done exit=%s elapsed=%.2fs' % (p.returncode, time.time()-started))
        return {'code':p.returncode,'stdout':p.stdout,'stderr':p.stderr}
    except subprocess.TimeoutExpired:
        trace(verbose, 'terminal timeout elapsed=%.2fs' % (time.time()-started))
        return {'code':124,'stdout':'','stderr':'command timeout'}

def visible_models(base, key):
    models = api(base, key, '/models', method='GET').get('data', [])
    def agent_model(model_id):
        name = model_id.lower()
        if any(x in name for x in ('embed','vision','safety','content-safety','parse','reward','diffusion','recurrent','omni')):
            return False
        return ('gemma-3-' in name or 'gemma-4-' in name or
                'gpt-oss' in name or
                'nemotron' in name and any(x in name for x in ('instruct','super','ultra','lightning','nano-3')))
    return [m['id'] for m in models if m.get('id') and agent_model(m['id'])]

def choose_model(models):
    for number, model in enumerate(models, 1): print('%3d %s' % (number, model))
    selected = input('Modelo [1]: ').strip() or '1'
    return models[int(selected)-1]

def main():
    parser = argparse.ArgumentParser(prog='cki-lite')
    parser.add_argument('--base-url', default=os.getenv('NIM_BASE_URL','https://integrate.api.nvidia.com/v1'))
    parser.add_argument('--key', help='NVIDIA API key; prefer NVIDIA_API_KEY instead')
    parser.add_argument('--model')
    parser.add_argument('--list-models', action='store_true')
    parser.add_argument('--verbose', action='store_true', help='show agent loop and tool execution trace')
    parser.add_argument('--session', help='resume a saved session')
    parser.add_argument('--export', metavar='FILE', help='export saved session(s) to JSON')
    args = parser.parse_args()
    if args.export:
        export_sessions(args.export, args.session); return
    key = args.key or os.getenv('NVIDIA_API_KEY') or getpass.getpass('NVIDIA API key: ')
    models = visible_models(args.base_url, key)
    if args.list_models:
        print('\n'.join(models)); return
    session_id = args.session or time.strftime('%Y%m%d-%H%M%S')+'-'+uuid.uuid4().hex[:6]
    saved = load_session(session_id) if args.session else None
    model = (saved or {}).get('model') or args.model or choose_model(models)
    print('cki-lite | %s | terminal agent enabled' % model)
    history = (saved or {}).get('messages', [])
    started_at = (saved or {}).get('started_at') or time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime())
    print('session: '+session_id)
    while True:
        try: prompt = input('\nVocê> ').strip()
        except (EOFError, KeyboardInterrupt): break
        if prompt in ('/quit','/exit'): save_session(session_id,model,history,started_at); break
        if prompt == '/clear': history = []; save_session(session_id,model,history,started_at); continue
        if prompt == '/save': save_session(session_id,model,history,started_at); print('Sessão salva: '+session_id); continue
        if prompt == '/export': export_sessions(session_id+'.json',session_id); continue
        if prompt == '/terminal': print(shell({'command':input('shell> ')}, args.verbose)); continue
        history.append({'role':'user','content':prompt})
        for loop in range(8):
            trace(args.verbose, 'agent loop=%d model=%s messages=%d' % (loop+1, model, len(history)))
            started = time.time()
            retries = 0; result = None; error = None
            while True:
                try:
                    result = api(args.base_url, key, '/chat/completions', {'model':model,'messages':history,'tools':[TOOL],'tool_choice':'auto','temperature':.2,'max_tokens':4096})
                    break
                except Exception as error:
                    if is_rate_limit(error):
                        retries += 1; delay = retries * 5
                        trace(args.verbose, 'rate limit; retry=%d wait=%ss model=%s' % (retries, delay, model))
                        print('[rate-limit] %s; retrying in %ss (attempt %d)' % (model, delay, retries), flush=True)
                        time.sleep(delay); continue
                    break
            if not isinstance(result, dict):
                trace(args.verbose, 'model error elapsed=%.2fs' % (time.time()-started))
                print('\nNIM error on %s: %s' % (model, error)); switched = False
                for candidate in models:
                    if candidate == model: continue
                    try:
                        result = api(args.base_url, key, '/chat/completions', {'model':candidate,'messages':history,'tools':[TOOL],'tool_choice':'auto','temperature':.2,'max_tokens':4096})
                        model = candidate; switched = True; print('[auto] continuing with %s' % model); break
                    except Exception as fallback_error: print('[auto] %s failed: %s' % (candidate, fallback_error))
                if not switched: print('[auto] no available Gemma/Nemotron model; task paused.'); break
            message = result['choices'][0]['message']; history.append(message); calls = message.get('tool_calls', [])
            trace(args.verbose, 'model response elapsed=%.2fs tool_calls=%d' % (time.time()-started, len(calls)))
            if not calls: print('\nNIM> ' + (message.get('content') or '')); break
            for call in calls:
                try: arguments = json.loads(call['function']['arguments'])
                except Exception: arguments = {'command':'echo invalid tool arguments'}
                print('[tool] ' + arguments.get('command','')); output = shell(arguments, args.verbose); print(output['stdout']+output['stderr'], end='')
                history.append({'role':'tool','tool_call_id':call['id'],'content':json.dumps(output)})
            save_session(session_id,model,history,started_at)

if __name__ == '__main__': main()
