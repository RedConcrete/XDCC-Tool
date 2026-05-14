import sys
sys.path.insert(0, '/app')
from downloader import search_beast_irc

queries = [
    'Guardians of the Galaxy',
    'Guardians Galaxy 2',
    'Ant-Man',
    'Ant-Man Wasp',
    'Black Panther',
    'Doctor Strange',
    'Spider-Man Homecoming',
    'Spider-Man Far From Home',
    'Spider-Man No Way Home',
    'Thor Ragnarok',
    'Captain America Civil War',
    'Captain America Return',
    'The Incredible Hulk',
    'Black Widow',
    'Shang-Chi',
    'Eternals',
]
for q in queries:
    results = search_beast_irc(q, lang='German')
    best = [r for r in results if r.get('size',0)//1024//1024 > 500
            and 'remux' not in r.get('fname','').lower()
            and '.dv.' not in r.get('fname','').lower()]
    if best:
        r = sorted(best, key=lambda x: x.get('size',0))[0]
        print(f"{r.get('size',0)//1024//1024} MB | {r.get('fname','?')}")
    else:
        print(f"NICHT GEFUNDEN: {q}")
