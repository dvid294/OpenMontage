#!/usr/bin/env python3
"""Rankly ranking-Short builder.

Usage: python3 make_ranking.py spec.json out.mp4

spec.json:
{
  "title": "RANKING SPEED'S CRAZIEST CHALLENGES",
  "hook": "Number 1 is insane...",
  "items": [  # ordered #5 -> #1
    {"clip": "a.mp4", "start": 0, "dur": 7, "label": "Silence challenge", "voice": "Number five. He lasted 3 seconds."},
    ...
  ],
  "outro": "Which one is your number 1? Comment below!"
}
Layout (1080x1920): top panel with title + numbered list that fills in as each
clip plays, clip below, short voice line per item, clip audio ducked under voice.
"""
import asyncio, json, os, subprocess, sys, tempfile

FONT = "/usr/share/fonts/opentype/inter/InterDisplay-Bold.otf"
VOICE = "en-US-GuyNeural"
W, H, TOP = 1080, 1920, 600
COLORS = ["0xFFD400", "0xC0C0C0", "0xCD7F32", "white", "white"]  # #1..#5


def run(cmd):
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def dur(path):
    out = subprocess.check_output(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                   "-of", "csv=p=0", path])
    return float(out)


async def tts(text, path):
    import ssl, edge_tts, edge_tts.communicate as ec
    ca = os.environ.get("SSL_CERT_FILE")
    if ca:
        ec._SSL_CTX = ssl.create_default_context(cafile=ca)
    await edge_tts.Communicate(text, VOICE, rate="+12%").save(path)


def esc(t):
    return t.replace("\\", "\\\\").replace(":", "\\:").replace("'", "’").replace("%", "\\%")


def main(spec_path, out):
    spec = json.load(open(spec_path))
    base = os.path.dirname(os.path.abspath(spec_path))
    tmp = tempfile.mkdtemp()
    items = spec["items"]
    n = len(items)
    segs, starts, t = [], [], 0.0
    for i, it in enumerate(items):
        rank = n - i
        voice = os.path.join(tmp, f"v{i}.mp3")
        asyncio.run(tts(it["voice"], voice))
        vd = dur(voice)
        d = max(it.get("dur", 7), vd + 0.6)
        seg = os.path.join(tmp, f"s{i}.mp4")
        clip = os.path.join(base, it["clip"])
        # clip fills the area below the panel; voice over ducked clip audio
        fc = (f"[0:v]scale={W}:{H-TOP}:force_original_aspect_ratio=increase,crop={W}:{H-TOP},"
              f"setsar=1,fps=30,pad={W}:{H}:0:{TOP}:black[v];"
              f"[0:a]aresample=44100,volume=0.35[ca];[1:a]aresample=44100,adelay=150|150,volume=1.6[va];"
              f"[ca][va]amix=inputs=2:duration=first:normalize=0[a]")
        run(["ffmpeg", "-y", "-ss", str(it.get("start", 0)), "-t", f"{d:.2f}", "-i", clip, "-i", voice,
             "-filter_complex", fc, "-map", "[v]", "-map", "[a]", "-t", f"{d:.2f}",
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-c:a", "aac", "-ar", "44100", seg])
        segs.append(seg); starts.append(t); t += d
    total = t
    lst = os.path.join(tmp, "l.txt")
    open(lst, "w").write("".join(f"file '{s}'\n" for s in segs))
    cat = os.path.join(tmp, "cat.mp4")
    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", lst, "-c", "copy", cat])

    # overlay: title, numbered list (labels reveal when their clip starts), current-rank badge, hook, outro
    dt = [f"drawtext=fontfile={FONT}:text='{esc(spec['title'])}':fontcolor=white:fontsize=58:"
          f"x=(w-tw)/2:y=40:borderw=4:bordercolor=black"]
    for i, it in enumerate(items):
        rank = n - i
        y = 140 + (rank - 1) * 88
        col = COLORS[rank - 1]
        dt.append(f"drawtext=fontfile={FONT}:text='{rank}.':fontcolor={col}:fontsize=60:x=60:y={y}:"
                  f"borderw=3:bordercolor=black")
        dt.append(f"drawtext=fontfile={FONT}:text='{esc(it['label'])}':fontcolor={col}:fontsize=54:x=150:y={y+4}:"
                  f"borderw=3:bordercolor=black:enable='gte(t,{starts[i]:.2f})'")
        dt.append(f"drawtext=fontfile={FONT}:text='#{rank}':fontcolor={col}:fontsize=150:x=w-tw-40:y={TOP+30}:"
                  f"borderw=8:bordercolor=black:enable='between(t,{starts[i]:.2f},{starts[i]+1.6:.2f})'")
    if spec.get("hook"):
        dt.append(f"drawtext=fontfile={FONT}:text='{esc(spec['hook'])}':fontcolor=0xFFD400:fontsize=70:"
                  f"x=(w-tw)/2:y=h*0.72:borderw=6:bordercolor=black:enable='lt(t,2.2)'")
    if spec.get("outro"):
        dt.append(f"drawtext=fontfile={FONT}:text='{esc(spec['outro'])}':fontcolor=white:fontsize=56:"
                  f"x=(w-tw)/2:y=h*0.80:borderw=6:bordercolor=black:box=1:boxcolor=0xE00000@0.85:boxborderw=20:"
                  f"enable='gte(t,{total-2.5:.2f})'")
    run(["ffmpeg", "-y", "-i", cat, "-vf", ",".join(dt), "-c:v", "libx264", "-preset", "medium",
         "-crf", "19", "-c:a", "copy", "-movflags", "+faststart", out])
    print(f"done {out} {total:.1f}s")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
