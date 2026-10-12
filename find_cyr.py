import re, os, sys

cyrillic = re.compile('[\u0400-\u04FF]')
skip_dirs = {'node_modules', '.git', 'build', 'dist', '__pycache__', '.vs', 'Release', 'Debug'}
ext = {'.h', '.cpp', '.py', '.ps1', '.md', '.js', '.ts', '.json', '.txt', '.bat', '.sh', '.yaml', '.yml'}

found_files = set()
lines_found = []

for root, dirs, files in os.walk('.'):
    dirs[:] = [d for d in dirs if d not in skip_dirs]
    for f in files:
        if os.path.splitext(f)[1].lower() not in ext:
            continue
        path = os.path.join(root, f)
        try:
            with open(path, encoding='utf-8', errors='replace') as fh:
                for i, line in enumerate(fh, 1):
                    if cyrillic.search(line):
                        found_files.add(path)
                        lines_found.append((path, i, line.rstrip()[:120]))
        except Exception as e:
            print(f'ERROR {path}: {e}', file=sys.stderr)

if lines_found:
    print(f'\n=== {len(lines_found)} Cyrillic lines in {len(found_files)} file(s) ===\n')
    cur = None
    for path, lineno, line in lines_found:
        if path != cur:
            print(f'\n--- {path} ---')
            cur = path
        print(f'  {lineno}: {line}')
else:
    print('All clear — no Cyrillic found.')
