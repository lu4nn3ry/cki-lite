#!/usr/bin/env python3
"""Minimal NVIDIA NIM terminal agent for Alpine and other Unix-like systems."""
import argparse, getpass, json, os, shlex, shutil, subprocess, time, urllib.request, urllib.error, uuid, hashlib, re, sys

COLOR = False
VERSION = '0.2.0'
MAX_OUTPUT_CHARS = 12000
MAX_HISTORY_CHARS = 60000

def clean_terminal(text):
    # Preserve text and line breaks, but not provider-controlled terminal escapes.
    text = re.sub(r'\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)', '', str(text))
    text = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', text)
    return re.sub(r'[\x00-\x08\x0b-\x1f\x7f]', '', text)

def paint(text, style):
    return '\x1b[' + style + 'm' + text + '\x1b[0m' if COLOR else text

def command_text(command):
    command = clean_terminal(command)
    # Highlight executable positions; arguments and results have separate styles.
    return re.sub(r'(^|[;|&]\s*|\n)(\s*)([^\s;|&]+)',
                  lambda m: m[1]+m[2]+paint(m[3], '1;36'), command)

def inline_markdown(text):
    pattern = r'(`+)(.+?)\1|\*\*(.+?)\*\*|__(.+?)__|\[([^\]]+)\]\(([^)]+)\)|(?<!\w)\*([^*\n]+)\*(?!\w)'
    def replace(m):
        if m[2] is not None: return paint(m[2], '36')
        if m[3] is not None or m[4] is not None: return paint(m[3] or m[4], '1')
        if m[5] is not None: return paint(m[5], '4;34')+' ('+m[6]+')'
        return paint(m[7], '3')
    return re.sub(pattern, replace, text)

def markdown(text):
    lines=[]; fence=None; language=''
    for line in clean_terminal(text).splitlines():
        marker=re.match(r'^\s*(`{3,}|~{3,})(.*)$',line)
        if marker and (fence is None or marker[1][0]==fence):
            if fence is None:
                fence=marker[1][0]; language=marker[2].strip().lower()
                lines.append(paint('  [code'+(': '+language if language else '')+']','2'))
            else: fence=None
            continue
        if fence:
            lines.append('  '+(command_text(line) if language in ('sh','shell','bash','ash','console') else paint(line,'36')))
            continue
        heading=re.match(r'^#{1,6}\s+(.+?)(?:\s+#+)?$',line)
        if heading: lines.append(paint(inline_markdown(heading[1]),'1;35')); continue
        if re.match(r'^\s*(?:---+|\*\*\*+)\s*$',line): lines.append(paint('─'*32,'2')); continue
        line=re.sub(r'^(\s*)[-*+]\s+',r'\1• ',line)
        if line.startswith('> '): line='│ '+line[2:]
        lines.append(inline_markdown(line))
    return '\n'.join(lines)

def show_output(output):
    for field,label,style in [('stdout','stdout','32'),('stderr','stderr','31')]:
        if output.get(field):
            print(paint('['+label+']',style))
            print(paint(clean_terminal(output[field]).rstrip('\n'),style))
    print(paint('[exit %s]' % output['code'],'2' if output['code']==0 else '1;31'))

def truncate_text(text, limit=MAX_OUTPUT_CHARS):
    text = str(text or '')
    if len(text) <= limit:
        return text
    head = max(1, int(limit * .75))
    tail = max(1, limit - head)
    omitted = len(text) - head - tail
    return text[:head] + '\n... [%d chars truncated] ...\n' % omitted + text[-tail:]

def limit_history(history, limit=MAX_HISTORY_CHARS):
    """Drop oldest messages until the request stays within a predictable bound."""
    while len(history) > 1 and len(json.dumps(history, ensure_ascii=False)) > limit:
        del history[0]

def show_command(command):
    print(paint('[terminal] ','1;33')+command_text(command),flush=True)

READ_ONLY_COMMANDS = {
    'basename', 'cat', 'cmp', 'cut', 'df', 'diff', 'dirname', 'du', 'echo', 'env', 'file', 'find',
    'grep', 'head', 'id', 'ls', 'md5sum', 'printf', 'pwd', 'readlink',
    'rg', 'sed', 'sort', 'stat', 'tail', 'test', 'time', 'tr', 'uname',
    'hostname', 'ps', 'true', 'uniq', 'wc', 'which', 'whoami', 'xargs'
}
DESTRUCTIVE_COMMANDS = {
    'dd', 'mkfs', 'mkfs.ext4', 'mkfs.xfs', 'mkswap', 'reboot', 'shutdown',
    'poweroff', 'halt', 'kill', 'pkill', 'killall'
}
MUTATING_COMMANDS = {
    'apk', 'apt', 'apt-get', 'chmod', 'chown', 'cp', 'install', 'mkdir',
    'mv', 'npm', 'pip', 'python', 'python3', 'rm', 'rmdir', 'sed', 'tee',
    'touch', 'truncate', 'wget'
}

def command_risk(command):
    """Return read, mutate, or destroy using a conservative shell heuristic."""
    text = str(command or '').strip()
    if not text:
        return 'read'
    lowered = text.lower()
    if re.search(r'(^|[|;&])\s*(curl|wget)\b[^\n]*\|\s*(sh|bash|ash)\b', lowered):
        return 'destroy'
    if re.search(r'(^|[|;&])\s*(rm|dd|mkfs|mkswap)\b[^\n]*\s(-[^\n]*r|--recursive|--force|of=)', lowered):
        return 'destroy'
    if re.search(r'\bgit\s+(reset\s+--hard|clean\b|push\b|checkout\s+--)', lowered):
        return 'destroy'
    if re.search(r'(^|[|;&])\s*(reboot|shutdown|poweroff|halt|kill|pkill|killall)\b', lowered):
        return 'destroy'
    if re.search(r'(^|[|;&])\s*(sudo|doas)\b', lowered) or re.search(r'(^|\s)(>|>>)\s*', text):
        return 'mutate'
    if re.search(r'\bxargs\b[^\n]*\b(rm|mv|cp|chmod|chown)\b', lowered):
        return 'destroy' if re.search(r'\bxargs\b[^\n]*\brm\b', lowered) else 'mutate'
    if re.search(r'\bfind\b[^\n]*\s-(delete|exec|execdir)\b', lowered):
        return 'destroy' if re.search(r'\bfind\b[^\n]*\s-delete\b', lowered) else 'mutate'
    try:
        tokens = shlex.split(text, posix=True)
    except ValueError:
        return 'mutate'
    if any(token in ('&&', '||', ';', '|') for token in tokens):
        return 'mutate'
    executable = os.path.basename(tokens[0]) if tokens else ''
    if executable in DESTRUCTIVE_COMMANDS:
        return 'destroy'
    if executable == 'git':
        if len(tokens) > 1 and tokens[1] in ('status', 'diff', 'log', 'show'):
            return 'read'
        return 'mutate'
    if executable == 'sed' and not any(token == '-i' or token.startswith('--in-place') for token in tokens[1:]):
        return 'read'
    if executable in MUTATING_COMMANDS:
        return 'mutate'
    if executable not in READ_ONLY_COMMANDS:
        return 'mutate'
    return 'read'

def confirm_command(command):
    risk = command_risk(command)
    if risk == 'read':
        return True
    if not sys.stdin.isatty():
        print('[blocked] command requires confirmation in an interactive terminal', flush=True)
        return False
    if risk == 'destroy':
        print('[warning] destructive command. Type "yes, I understand" to continue:', flush=True)
        return input('confirm> ').strip().lower() == 'yes, i understand'
    return input('Allow this mutating command? [y/N] ').strip().lower() in ('y', 'yes')

TOOL = {'type':'function','function':{'name':'terminal','description':'Execute shell commands on this host for the user request.','parameters':{'type':'object','properties':{'command':{'type':'string'},'cwd':{'type':'string'},'timeout':{'type':'integer'}},'required':['command']}}}

def api(base, key, path, data=None, method='POST'):
    raw = None if data is None else json.dumps(data).encode()
    req = urllib.request.Request(base.rstrip('/') + path, raw, method=method, headers={'Authorization':'Bearer '+key,'Content-Type':'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=180) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as error:
        detail = error.read().decode('utf-8','replace').replace(key,'[REDACTED]')
        failure = RuntimeError('HTTP %s: %s' % (error.code, detail[:500]))
        failure.code = error.code
        raise failure from None

def api_stream(base, key, path, data, on_text=None):
    payload = dict(data); payload['stream'] = True
    req = urllib.request.Request(base.rstrip('/') + path, json.dumps(payload).encode(),
                                 headers={'Authorization':'Bearer '+key,'Content-Type':'application/json'})
    message = {'role':'assistant','content':'','tool_calls':[]}
    try:
        with urllib.request.urlopen(req, timeout=180) as response:
            for raw_line in response:
                line = raw_line.decode('utf-8','replace').strip()
                if not line.startswith('data:'):
                    continue
                chunk = line[5:].strip()
                if chunk == '[DONE]':
                    break
                try: delta = json.loads(chunk).get('choices',[{}])[0].get('delta',{})
                except (ValueError, IndexError, AttributeError): continue
                text = delta.get('content') or ''
                if text:
                    message['content'] += text
                    if on_text: on_text(text)
                for item in delta.get('tool_calls', []):
                    index = item.get('index', 0)
                    while len(message['tool_calls']) <= index:
                        message['tool_calls'].append({'id':'','type':'function','function':{'name':'','arguments':''}})
                    target = message['tool_calls'][index]
                    target['id'] += item.get('id') or ''
                    function = item.get('function') or {}
                    target['function']['name'] += function.get('name') or ''
                    target['function']['arguments'] += function.get('arguments') or ''
        if not message['tool_calls']: message.pop('tool_calls')
        return {'choices':[{'message':message}]}
    except urllib.error.HTTPError as error:
        detail = error.read().decode('utf-8','replace').replace(key,'[REDACTED]')
        failure = RuntimeError('HTTP %s: %s' % (error.code, detail[:500])); failure.code = error.code
        raise failure from None

def chat_request(base, key, payload, stream_output=True):
    on_text = lambda text: print(clean_terminal(text), end='', flush=True) if stream_output else None
    return api_stream(base, key, '/chat/completions', payload, on_text)

def is_rate_limit(error):
    return getattr(error, 'code', None) == 429 or 'rate limit' in str(error).lower() or 'too many requests' in str(error).lower()

def trace(enabled, message):
    if enabled: print('[trace] ' + message, flush=True)

def session_dir():
    path = os.path.expanduser(os.getenv('CKI_LITE_HOME','~/.cki-lite'))
    os.makedirs(path, exist_ok=True)
    return path

def load_dotenv():
    paths = [os.path.join(os.path.dirname(os.path.realpath(__file__)), '.env'), os.path.join(session_dir(), '.env'), os.path.join(os.getcwd(), '.env')]
    for path in paths:
        if not os.path.exists(path): continue
        with open(path, encoding='utf-8') as env_file:
            for line in env_file:
                line=line.strip()
                if not line or line.startswith('#'): continue
                if line.startswith('export '): line=line[7:].strip()
                if '=' not in line: continue
                name,value=line.split('=',1); value=value.strip().strip('"').strip("'")
                if name.strip() and name.strip() not in os.environ: os.environ[name.strip()]=value

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
    if not confirm_command(command):
        return {'code':126,'stdout':'','stderr':'command not approved'}
    started = time.time(); trace(verbose, 'terminal start cwd=%s timeout=%ss' % (cwd, timeout))
    try:
        p = subprocess.run(['/bin/sh','-lc',command], cwd=cwd, text=True, capture_output=True, timeout=timeout)
        trace(verbose, 'terminal done exit=%s elapsed=%.2fs' % (p.returncode, time.time()-started))
        return {'code':p.returncode,'stdout':truncate_text(p.stdout),'stderr':truncate_text(p.stderr)}
    except subprocess.TimeoutExpired:
        trace(verbose, 'terminal timeout elapsed=%.2fs' % (time.time()-started))
        return {'code':124,'stdout':'','stderr':'command timeout'}

def visible_models(base, key, refresh=False, provider='nvidia'):
    cache_dir=os.path.join(session_dir(),'cache'); os.makedirs(cache_dir,exist_ok=True)
    cache=os.path.join(cache_dir,hashlib.sha256(base.encode()).hexdigest()[:16]+'.json')
    models=None
    if not refresh and os.path.exists(cache):
        try:
            with open(cache) as f: models=json.load(f)
        except (ValueError,OSError): pass
    if models is None:
        models = api(base, key, '/models', method='GET').get('data', [])
        with open(cache+'.tmp','w') as f: json.dump(models,f)
        os.replace(cache+'.tmp',cache)
    selected=os.path.join(cache_dir,'selected.json')
    if os.path.exists(selected):
        with open(selected) as f: ranked=json.load(f)
        available={m['id'] for m in models}
        return [m for m in ranked if m in available]
    def agent_model(model_id):
        name = model_id.lower()
        if any(x in name for x in ('embed','vision','safety','content-safety','parse','reward','diffusion','recurrent','omni')):
            return False
        if provider != 'nvidia':
            return True
        return ('gemma-3-' in name or 'gemma-4-' in name or
                'gpt-oss' in name or
                'nemotron' in name and any(x in name for x in ('instruct','super','ultra','lightning','nano-3')))
    return [m['id'] for m in models if m.get('id') and agent_model(m['id'])]

def benchmark_models(base, key, models):
    results = []
    for model in models:
        started = time.time()
        try:
            result = api(base, key, '/chat/completions', {
                'model':model, 'messages':[{'role':'user','content':'Reply with OK.'}],
                'temperature':0, 'max_tokens':8})
            if result.get('choices'):
                results.append((time.time()-started, model))
                print('benchmark %-48s %.2fs' % (model, time.time()-started))
        except Exception as error:
            print('benchmark %-48s failed: %s' % (model, error))
    results.sort()
    cache_dir = os.path.join(session_dir(),'cache'); os.makedirs(cache_dir,exist_ok=True)
    with open(os.path.join(cache_dir,'selected.json.tmp'),'w',encoding='utf-8') as f:
        json.dump([model for _,model in results], f)
    os.replace(os.path.join(cache_dir,'selected.json.tmp'), os.path.join(cache_dir,'selected.json'))
    return [model for _,model in results]

def install_self():
    target_dir = os.path.expanduser('~/.local/bin'); os.makedirs(target_dir, exist_ok=True)
    target = os.path.join(target_dir, 'cki-lite')
    shutil.copy2(os.path.realpath(__file__), target); os.chmod(target, 0o755)
    print('Installed: '+target)

PROVIDERS = {
    'nvidia': ('https://integrate.api.nvidia.com/v1', 'NVIDIA_API_KEY'),
    'ollama': ('http://localhost:11434/v1', 'OLLAMA_API_KEY'),
    'openrouter': ('https://openrouter.ai/api/v1', 'OPENROUTER_API_KEY'),
    'groq': ('https://api.groq.com/openai/v1', 'GROQ_API_KEY'),
    'gemini': ('https://generativelanguage.googleapis.com/v1beta/openai', 'GEMINI_API_KEY')
}

def provider_config(provider, base_url=None, key=None):
    default_base, key_name = PROVIDERS[provider]
    return (base_url or os.getenv('NIM_BASE_URL' if provider == 'nvidia' else provider.upper()+'_BASE_URL', default_base),
            key or os.getenv(key_name) or ('ollama' if provider == 'ollama' else None))

def choose_model(models):
    for number, model in enumerate(models, 1): print('%3d %s' % (number, model))
    selected = input('Modelo [1]: ').strip() or '1'
    return models[int(selected)-1]

def main():
    load_dotenv()
    parser = argparse.ArgumentParser(prog='cki-lite')
    parser.add_argument('--provider', choices=tuple(PROVIDERS), default=os.getenv('CKI_LITE_PROVIDER','nvidia'))
    parser.add_argument('--base-url')
    parser.add_argument('--key', help='NVIDIA API key; prefer NVIDIA_API_KEY instead')
    parser.add_argument('--model')
    parser.add_argument('--prompt', help='run one prompt and exit')
    parser.add_argument('--version', action='version', version='%(prog)s '+VERSION)
    parser.add_argument('--list-models', action='store_true')
    parser.add_argument('--refresh-models', action='store_true', help='refresh cached NVIDIA catalog')
    parser.add_argument('--benchmark-models', action='store_true', help='benchmark visible models and save ranking')
    parser.add_argument('--install', action='store_true', help='install executable to ~/.local/bin/cki-lite')
    parser.add_argument('--verbose', action='store_true', help='show agent loop and tool execution trace')
    parser.add_argument('--color', choices=('auto','always','never'), default='auto', help='terminal colors (default: auto)')
    parser.add_argument('--session', help='resume a saved session')
    parser.add_argument('--export', metavar='FILE', help='export saved session(s) to JSON')
    args = parser.parse_args()
    global COLOR
    COLOR = args.color == 'always' or (args.color == 'auto' and sys.stdout.isatty() and 'NO_COLOR' not in os.environ and os.getenv('TERM') != 'dumb')
    if args.export:
        export_sessions(args.export, args.session); return
    if args.install:
        install_self(); return
    args.base_url, key = provider_config(args.provider, args.base_url, args.key)
    if not key:
        key = getpass.getpass(args.provider.upper()+' API key: ')
    models = visible_models(args.base_url, key, args.refresh_models, args.provider)
    if args.benchmark_models:
        benchmark_models(args.base_url, key, models); return
    if args.list_models:
        print('\n'.join(models)); return
    session_id = args.session or time.strftime('%Y%m%d-%H%M%S')+'-'+uuid.uuid4().hex[:6]
    saved = load_session(session_id) if args.session else None
    model = (saved or {}).get('model') or args.model or choose_model(models)
    print('cki-lite | %s | terminal agent enabled' % model)
    history = (saved or {}).get('messages', [])
    started_at = (saved or {}).get('started_at') or time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime())
    print('session: '+session_id)
    exit_code = 0
    one_shot = args.prompt is not None
    while True:
        try: prompt = args.prompt if one_shot else input('\nVocê> ').strip()
        except (EOFError, KeyboardInterrupt): break
        one_shot = False
        if prompt in ('/quit','/exit'): save_session(session_id,model,history,started_at); break
        if prompt == '/clear': history = []; save_session(session_id,model,history,started_at); continue
        if prompt == '/save': save_session(session_id,model,history,started_at); print('Sessão salva: '+session_id); continue
        if prompt == '/export': export_sessions(session_id+'.json',session_id); continue
        if prompt == '/terminal':
            command=input('shell> '); show_command(command)
            show_output(shell({'command':command},args.verbose)); continue
        history.append({'role':'user','content':prompt})
        for loop in range(8):
            limit_history(history)
            trace(args.verbose, 'agent loop=%d model=%s messages=%d' % (loop+1, model, len(history)))
            started = time.time()
            retries = 0; result = None; last_error = None
            while True:
                try:
                    print('\n'+paint('NIM>','1;35')+' ', end='', flush=True)
                    result = chat_request(args.base_url, key, {'model':model,'messages':history,'tools':[TOOL],'tool_choice':'auto','temperature':.2,'max_tokens':4096})
                    break
                except Exception as request_error:
                    last_error = request_error
                    if is_rate_limit(request_error):
                        retries += 1; delay = retries * 5
                        trace(args.verbose, 'rate limit; retry=%d wait=%ss model=%s' % (retries, delay, model))
                        print('[rate-limit] %s; retrying in %ss (attempt %d)' % (model, delay, retries), flush=True)
                        time.sleep(delay); continue
                    break
            if not isinstance(result, dict):
                trace(args.verbose, 'model error elapsed=%.2fs' % (time.time()-started))
                print('\nNIM error on %s: %s' % (model, last_error)); switched = False
                for candidate in models:
                    if candidate == model: continue
                    try:
                        print('\n'+paint('NIM>','1;35')+' ', end='', flush=True)
                        result = chat_request(args.base_url, key, {'model':candidate,'messages':history,'tools':[TOOL],'tool_choice':'auto','temperature':.2,'max_tokens':4096})
                        model = candidate; switched = True; print('[auto] continuing with %s' % model); break
                    except Exception as fallback_error: print('[auto] %s failed: %s' % (candidate, fallback_error))
                if not switched:
                    print('[auto] no available Gemma/Nemotron model; task paused.')
                    exit_code = 1
                    break
            message = result['choices'][0]['message']; history.append(message); calls = message.get('tool_calls', [])
            trace(args.verbose, 'model response elapsed=%.2fs tool_calls=%d' % (time.time()-started, len(calls)))
            if not calls:
                save_session(session_id,model,history,started_at)
                print(); break
            for call in calls:
                try: arguments = json.loads(call['function']['arguments'])
                except Exception: arguments = {'command':'echo invalid tool arguments'}
                show_command(arguments.get('command','')); output = shell(arguments, args.verbose); show_output(output)
                history.append({'role':'tool','tool_call_id':call['id'],'content':json.dumps(output)})
            save_session(session_id,model,history,started_at)
        if args.prompt is not None:
            return exit_code

    return exit_code

if __name__ == '__main__':
    raise SystemExit(main() or 0)
