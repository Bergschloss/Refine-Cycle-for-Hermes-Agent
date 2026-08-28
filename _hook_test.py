import __init__
__init__._ACTIVE_BLOCK_RULES = [('building with make', 'USE PYTHON')]
for cmd in ['make build', 'python build.py', 'git commit -m fix', 'make clean', 'grep make']:
    r = __init__._on_pre_tool_call('terminal', {'command': cmd})
    print(f'{cmd:30s} -> {"BLOCK" if r else "-"}')
