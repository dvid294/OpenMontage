"""Rankly daily pipeline helpers — run INSIDE the Composio remote workbench.

The workbench preloads `upload_local_file` and `run_composio_tool`; this file
expects them as globals, so load it with:  exec(open('/home/user/rankly/rankly_lib.py').read())

Flow per video:
  setup()                                  # once per fresh sandbox
  rows = top_tiktoks('handle')             # [(id, views, secs, title)] sorted by views
  download('handle', ids)                  # -> {id: ok}
  sheet('handle', ids)                     # -> s3 url of a contact sheet (curl -L it locally and look)
  render_and_upload(key, spec, title, desc, tags)   # background; poll STATUS[key]

Why TikTok: YouTube blocks yt-dlp from cloud IPs, TikTok does not.
YouTube Data API quota allows ~6 uploads/day per project; post 5.
"""
import json, os, re, subprocess, sys, threading, time
from concurrent.futures import ThreadPoolExecutor

BASE = '/home/user/rankly'


def setup(builder_src=None):
    subprocess.run([sys.executable, '-m', 'pip', 'install', '-q', 'yt-dlp', 'edge-tts', 'imageio-ffmpeg'],
                   capture_output=True)
    import imageio_ffmpeg
    os.makedirs('/home/user/bin', exist_ok=True)
    os.makedirs(BASE, exist_ok=True)
    if not os.path.exists('/home/user/bin/ffmpeg'):
        os.symlink(imageio_ffmpeg.get_ffmpeg_exe(), '/home/user/bin/ffmpeg')
    os.environ['PATH'] = '/home/user/bin:' + os.environ['PATH']
    if builder_src:
        open(BASE + '/make_ranking.py', 'w').write(builder_src)


def top_tiktoks(handle, n=80, minlen=5, maxlen=60):
    r = subprocess.run(['yt-dlp', '--flat-playlist', '--print', '%(id)s|%(view_count)s|%(duration)s|%(title).70s',
                        '-I', f'1:{n}', f'https://www.tiktok.com/@{handle}'], capture_output=True, text=True, timeout=170)
    rows = [l.split('|', 3) for l in r.stdout.splitlines() if l.count('|') >= 3]
    rows = [(a, int(b), float(c), d) for a, b, c, d in rows
            if b.isdigit() and c not in ('NA', '') and minlen <= float(c) <= maxlen]
    return sorted(rows, key=lambda x: -x[1])


def download(handle, ids):
    d = f'{BASE}/{handle}'
    os.makedirs(d, exist_ok=True)

    def one(i):
        for _ in range(3):
            if subprocess.run(['yt-dlp', '-q', '-o', f'{d}/{i}.%(ext)s', f'https://www.tiktok.com/@{handle}/video/{i}'],
                              capture_output=True, timeout=170).returncode == 0:
                return i, True
        return i, False

    with ThreadPoolExecutor(5) as ex:
        return dict(ex.map(one, ids))


def sheet(handle, ids):
    rows = []
    for i in ids:
        o = f'{BASE}/{handle}/{i}_row.jpg'
        subprocess.run(['ffmpeg', '-v', 'error', '-y', '-i', f'{BASE}/{handle}/{i}.mp4', '-vf',
                        "select='not(mod(n\\,60))',scale=150:200:force_original_aspect_ratio=decrease,"
                        "pad=150:200:-1:-1,tile=6x1", '-frames:v', '1', o])
        rows.append(o)
    a = []
    for r in rows:
        a += ['-i', r]
    out = f'{BASE}/sheet_{handle}.jpg'
    subprocess.run(['ffmpeg', '-v', 'error', '-y', *a, '-filter_complex', f'vstack={len(rows)}', '-q:v', '5', out])
    res, err = upload_local_file(out)  # noqa: F821 (workbench helper)
    return err or res.get('data', res).get('s3_url')


STATUS = {}


def render_and_upload(key, spec, title, desc, tags, account='youtube_caving-ran'):
    def job():
        try:
            STATUS[key] = 'rendering'
            sp, out = f'{BASE}/{key}.json', f'{BASE}/{key}.mp4'
            json.dump(spec, open(sp, 'w'))
            r = subprocess.run([sys.executable, BASE + '/make_ranking.py', sp, out],
                               capture_output=True, text=True, timeout=900)
            if r.returncode:
                STATUS[key] = 'render_err ' + r.stderr[-400:]
                return
            STATUS[key] = 'uploading'
            res, err = upload_local_file(out)  # noqa: F821
            if err:
                STATUS[key] = 's3_err ' + str(err)
                return
            s3key = res.get('data', res).get('s3key')
            r2, e2 = run_composio_tool('YOUTUBE_UPLOAD_VIDEO', {  # noqa: F821
                'title': title, 'description': desc, 'tags': tags, 'categoryId': '24', 'privacyStatus': 'public',
                'videoFilePath': {'name': key + '.mp4', 'mimetype': 'video/mp4', 's3key': s3key}}, account=account)
            if e2:
                STATUS[key] = 'yt_err ' + str(e2)[:400]
                return
            m = re.search(r'"id": "([\w-]{11})"', json.dumps(r2))
            STATUS[key] = 'done ' + (m.group(1) if m else '?')
        except Exception as ex:  # surface everything to the poller
            STATUS[key] = 'exc ' + repr(ex)

    threading.Thread(target=job).start()
