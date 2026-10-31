#!/usr/bin/env python3
"""Builds one/static/builders.tgz: Show Builder and SuperSource Builder for the One.

    python3 tools/build-builders.py [--companion-tree]

The Mac apps stay the source. This copies what their macapp/build.sh files copy, then
applies the One's changes as asserted rewrites: if a string moved, the build stops
here instead of a lock or an ad shipping. On the One nothing is locked, nothing is
advertised and nothing phones home. Then it checks the result for what must be gone.

Show Builder comes from the Companion repo's HEAD (its working tree can hold another
session's unfinished work; --companion-tree takes it anyway). SuperSource Builder's
folder isn't a repo, so it comes from the working tree.

A tarball, not a folder: a One on v1.7.0 copies static/ on update but no new top-level
folders, and this keeps the builders' source out of the browsable public repo. The
app unpacks it on first open (app.py, /builders/start).
"""
import gzip, hashlib, io, os, re, shutil, subprocess, sys, tarfile, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
SYSTEMS = os.path.dirname(REPO)
COMPANION = os.path.join(SYSTEMS, "Companion")
SS = os.path.join(SYSTEMS, "ATEM SS Builder")
OUT = os.path.join(REPO, "one", "static", "builders.tgz")
FONTS = ("Rajdhani-Bold.ttf", "ShareTechMono-Regular.ttf", "Inter-Regular.ttf")

ONE_HOST = 'String(M.companion).replace(/^(127\\.0\\.0\\.1|localhost)/,location.hostname)'


def sub(text, pattern, new, what, flags=0, count=1):
    """A regex that must match, exactly `count` times."""
    got, n = re.subn(pattern, lambda _: new, text, flags=flags)
    if n != count:
        sys.exit(f"STOP: {what}: expected {count} match(es), found {n}. The source moved; update this script.")
    return got


def line(text, start, new, what):
    """Replace the whole line that begins with `start` (after its indent)."""
    return sub(text, r"(?m)^[ \t]*" + re.escape(start) + r".*$", new, what)


# On a One the builders stop when nobody has them open. Each open page says so once a minute, to both builders,
# because each opens the other. Asleep or stopped, the page asks the One to start them again, so an open tab keeps working.
ALIVE_JS = """<script>
(() => {
  const host = location.hostname;
  const alive = () => { for (const p of [8787, 8789]) fetch(`http://${host}:${p}/api/alive`, { mode: "no-cors", cache: "no-store" })
    .catch(() => fetch(`http://${host}:8080/builders/start`, { method: "POST", mode: "no-cors" }).catch(() => {})); };
  alive(); setInterval(alive, 60000);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) alive(); });
})();
</script>
</body>"""
# Any request counts. Nothing for 10 minutes, and the server leaves; /builders/start brings it back
IDLE_PY = """
BUILDER_IDLE = int(os.environ.get("DOWNSTAGE_BUILDER_IDLE") or 600)
last_request = [time.time()]
def idle_watch():
    while True:
        time.sleep(min(10, BUILDER_IDLE))
        if time.time() - last_request[0] > BUILDER_IDLE:
            os._exit(0)
threading.Thread(target=idle_watch, daemon=True).start()

"""
ALIVE_GET = """    def do_GET(self):
        last_request[0] = time.time()
        if self.path == "/api/alive":
            return self.send(200, {"ok": True})
"""
ALIVE_POST = """    def do_POST(self):
        last_request[0] = time.time()
"""


def stops_when_idle(t, cls, what):
    t = sub(t, r"(?m)^class " + cls + r"\(BaseHTTPRequestHandler\):", IDLE_PY.lstrip("\n") + "class " + cls + "(BaseHTTPRequestHandler):", what + ": idle watch")
    t = sub(t, r"    def do_GET\(self\):\n", ALIVE_GET, what + ": GET")
    return sub(t, r"    def do_POST\(self\):\n", ALIVE_POST, what + ": POST")


def show_html(t):
    t = line(t, '<a class="btn small sitelink"', "", "Show Builder: website link")
    t = sub(t, r'let S = \{name:"ATEM SHOW", deck:"plusxl", cells:\{\}, backPage:1, output:"page"', 'let S = {name:"ATEM SHOW", deck:"plusxl", cells:{}, backPage:1, output:"send"', "Show Builder: send by default")
    t = sub(t, r'<section class="card" data-g="one" style="--cc:#2bb5b8">\s*<h2>Get a Downstage One</h2>.*?</section>', "",
            "Show Builder: Get a Downstage One card", re.S)
    t = sub(t, r'<section class="card suggest".*?</section>', "", "Show Builder: suggest card", re.S)
    t = sub(t, r'function openOnePop\(\)\{.*?(?=\(async \(\)=>\{\n  load\(\);)', "", "Show Builder: One popup and flyer", re.S)
    t = sub(t, r'<button class="btn small" id="quit">', '<button class="btn small" id="quit" style="display:none">', "Show Builder: Quit")
    t = sub(t, r'<label class="field">Companion <input id="host" size="18"></label>',
            '<label class="field" style="display:none">Companion <input id="host" size="18"></label>', "Show Builder: Companion address")
    t = sub(t, r'href="http://\$\{esc\(M\.companion\)\}/buttons', 'href="http://${esc(' + ONE_HOST + ')}/buttons', "Show Builder: Open in Companion")
    t = sub(t, r'<button class="btn small" data-showfolder [^>]*>Show folder</button>', "", "Show Builder: Show folder")
    # a show can leave the One: Open... already takes a file from this computer
    t = sub(t, r'(<button class="btn small" data-showcmd="saveas" title="Save a copy under a new name">Save As…</button>)',
            '<button class="btn small" data-showcmd="saveas" title="Save a copy under a new name">Save As…</button>\n'
            '      <button class="btn small" data-showcmd="download" title="Download this show to this computer, to keep it or move it to another One">Download</button>', "Show Builder: Download button")
    t = sub(t, r'if\(cmd==="open"\)\{ if\(showBridge\)',
            'if(cmd==="download"){ const a=document.createElement("a"); a.href=URL.createObjectURL(new Blob([JSON.stringify(showDoc(),null,1)],{type:"application/octet-stream"})); '
            'a.download=showFilename(); document.body.appendChild(a); a.click(); a.remove(); setTimeout(()=>URL.revokeObjectURL(a.href),60000); return; }\n'
            '  if(cmd==="open"){ if(showBridge)', "Show Builder: download command")
    # exports land in the browser's own downloads
    t = sub(t, r'\$\("#reveal"\)\.onclick = \(\)=>api\("/api/reveal",\{path:r\.path\}\);',
            '$("#reveal").onclick = ()=>{ location.href="/api/download?file="+encodeURIComponent(r.file); }; $("#reveal").click();', "Show Builder: merge download")
    t = sub(t, r'\$\("#reveal"\)\.onclick = \(\)=>api\("/api/reveal",\{path:last\.path\}\);',
            'files.forEach((f,i)=>setTimeout(()=>{ const a=document.createElement("a"); a.href="/api/download?file="+encodeURIComponent(f); a.download=f; document.body.appendChild(a); a.click(); a.remove(); },i*400)); '
            '$("#reveal").onclick = ()=>{ location.href="/api/download?file="+encodeURIComponent(last.file); };', "Show Builder: page download")
    t = sub(t, r'>Show in Finder<', '>Download again<', "Show Builder: reveal label", count=t.count(">Show in Finder<"))
    t = sub(t, r'Saved to Downloads:', 'Downloaded:', "Show Builder: saved wording", count=t.count("Saved to Downloads:"))
    t = sub(t, r"</body>", ALIVE_JS, "Show Builder: heartbeat")
    return t.replace("on this Mac", "on this One")


def show_py(t):
    t = sub(t, r'SS_BUILDER = os\.path\.join\(os\.path\.dirname\(PROJECT\), "ATEM SS Builder"\)\nif DATA:.*?\n_SSLOOKS = \{\}',
            'SS_BUILDER = os.environ["DOWNSTAGE_SS_DATA"]   # the One: SuperSource Builder\'s data folder here\n_SSLOOKS = {}',
            "Show Builder: SS_BUILDER", re.S)
    t = sub(t, r'SHOW_FOLDER = os\.path\.expanduser\("~/Documents/Downstage Shows"\)', 'SHOW_FOLDER = os.path.join(DATA, "Downstage Shows")', "Show Builder: show folder")
    t = sub(t, r'DOWNLOADS = os\.path\.expanduser\("~/Downloads"\)',
            'DOWNLOADS = os.path.join(DATA, "downloads")\nos.makedirs(DOWNLOADS, exist_ok=True)', "Show Builder: downloads")
    t = sub(t, r'if self\.path == "/one-photo\.webp":\n\s*return self\.send\(200, open\(os\.path\.join\(HERE, "one-photo\.webp"\), "rb"\)\.read\(\), "image/webp"\)\n',
            'if self.path.startswith("/api/download?"):\n'
            '                q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)\n'
            '                f = os.path.basename((q.get("file") or [""])[0])\n'
            '                data = open(os.path.join(DOWNLOADS, f), "rb").read()\n'
            '                self.send_response(200)\n'
            '                self.send_header("Content-Type", "application/octet-stream")\n'
            '                self.send_header("Content-Disposition", "attachment; filename*=UTF-8\'\'" + urllib.parse.quote(f))\n'
            '                self.send_header("Content-Length", str(len(data)))\n'
            '                self.end_headers()\n'
            '                self.wfile.write(data)\n'
            '                return\n', "Show Builder: photo route, download route")
    t = sub(t, r'if self\.path == "/api/open-ss-builder":', 'if False:', "Show Builder: open SS", count=t.count('self.path == "/api/open-ss-builder"'))
    t = sub(t, r'if self\.path == "/api/quit":', 'if False:   # the One keeps it running', "Show Builder: quit route")
    t = sub(t, r'subprocess\.Popen\(\["open", SHOW_FOLDER\]\)', 'pass', "Show Builder: show folder route")
    t = sub(t, r'threading\.Thread\(target=watch_parent, daemon=True\)\.start\(\)', 'pass   # the One\'s service owns this process', "Show Builder: parent watcher")
    t = sub(t, r'srv = ThreadingHTTPServer\(\("127\.0\.0\.1", a\.port\), Handler\)', 'srv = ThreadingHTTPServer(("0.0.0.0", a.port), Handler)', "Show Builder: bind")
    return stops_when_idle(t, "Handler", "Show Builder")


def me_pages(t):
    # index 1 of Helvetica.ttc is the bold cut: Liberation Sans Bold has the same widths
    return sub(t, r'def font\(sz\):\n    for p in \("/System/Library/Fonts/Helvetica\.ttc", "/System/Library/Fonts/HelveticaNeue\.ttc"\):\n        try:\n            return ImageFont\.truetype\(p, sz, index=1\)',
               'def font(sz):\n    for p in ("/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"):\n        try:\n            return ImageFont.truetype(p, sz)',
               "atem-me-pages: key font")


def ss_html(t):
    t = sub(t, r'let BONUS = false;', 'let BONUS = true;', "SS: bonus")
    t = line(t, '<a class="btn small sitelink"', "", "SS: website link")
    t = sub(t, r'<p class="hint" id="bonusText" style="margin-bottom:4px"></p><button class="btn small" id="bonusBtn">Unlock the bonus…</button>',
            '<p class="hint hidden" id="bonusText"></p><button class="btn small hidden" id="bonusBtn"></button>', "SS: unlock row")
    t = sub(t, r'function renderBonusRow\(\) \{.*?\n\}\n', 'function renderBonusRow() {}\n', "SS: bonus row text", re.S)
    t = sub(t, r'\{ let n = 0, t = null; document\.querySelector\("header \.mark"\)\.addEventListener\("click".*?\n\}\); \}\n', "", "SS: 5-click bonus toggle", re.S)
    t = sub(t, r'function askUnlock\(then\) \{.*?\n\}\n(?=// ---- box frames)', 'function askUnlock(then) { if (then) then(); }\n', "SS: unlock dialog", re.S)
    t = sub(t, r' Patterns with a padlock, and the complete <b>look packs</b> in Templates \(Keynote, Awards, Worship, Conference, Holiday\), unlock on this Mac when you enter your email once\.',
            ' The complete <b>look packs</b> are in Templates (Keynote, Awards, Worship, Conference, Holiday).', "SS: guide sentence")
    t = sub(t, r'Writes files you load yourself\. It only reads a switcher or sends to Companion when you press\. <a href="https://downstage\.systems/" target="_blank" rel="noopener">downstage\.systems</a>',
            "", "SS: footer")
    t = sub(t, r'const openShowBuilder = \(\) => api\("/api/open-show-builder", \{\}\);',
            'const openShowBuilder = () => window.open(`http://${location.hostname}:8787/`, "_blank", "noopener");', "SS: open Show Builder")
    t = sub(t, r'\$\("reveal"\)\.onclick = \(\) => api\("/api/reveal", \{ path: r\.folder \}\);',
            '$("reveal").onclick = () => { location.href = "/api/exportzip?name=" + encodeURIComponent(exportName()); };', "SS: export download")
    t = sub(t, r'<button class="btn small" id="reveal">Show in Finder</button>', '<button class="btn small primary" id="reveal">Download the export (zip)</button>', "SS: reveal label")
    # a show can leave the One and come back: the file holds every look and its pictures
    t = sub(t, r'<button class="btn small" id="saveTpl" title="Keep this look as a starting point in New show › My templates">Save as template</button>',
            '<button class="btn small" id="saveTpl" title="Keep this look as a starting point in New show › My templates">Save as template</button>'
            '<button class="btn small" id="downloadShow" title="Download this show to this computer, to keep it or move it to another One">Download show</button>'
            '<button class="btn small" id="openShowFile" title="Open a show file from this computer">Open show file…</button>'
            '<input type="file" id="showFileInput" accept=".json" class="hidden">', "SS: download buttons")
    t = sub(t, r'  const saved = await api\("/api/shows/" \+ encodeURIComponent\(e\.target\.value\)\);\n'
               r'  if \(saved\.seconds === undefined\) saved\.seconds = null;\n'
               r'(  L = Object\.assign\(defaultLayout\(\), saved, .*?\n.*?\n)'
               r'  e\.target\.value = "";\n'
               r'  await refreshMeta\(L\.profile\);\n'
               r'  try \{ await loadProfile\(L\.profile \|\| TEMPLATE\); \} catch \{ await loadProfile\(TEMPLATE\); \}\n'
               r'  sel = \{ kind: "box", i: 0 \}; renderAll\(\);\n\};\n',
            '  const saved = await api("/api/shows/" + encodeURIComponent(e.target.value));\n'
            '  e.target.value = "";\n'
            '  await useSavedShow(saved);\n'
            '};\n'
            'async function useSavedShow(saved) {\n'
            '  if (saved.seconds === undefined) saved.seconds = null;\n'
            '  L = Object.assign(defaultLayout(), saved, { bg: { ...defaultLayout().bg, ...(saved.bg || {}) },\n'
            '                                              snap: { ...defaultLayout().snap, ...(saved.snap || {}) } });\n'
            '  await refreshMeta(L.profile);\n'
            '  try { await loadProfile(L.profile || TEMPLATE); } catch { await loadProfile(TEMPLATE); }\n'
            '  sel = { kind: "box", i: 0 }; renderAll();\n'
            '}\n'
            '$("downloadShow").onclick = () => {\n'
            '  const a = document.createElement("a");\n'
            '  a.href = URL.createObjectURL(new Blob([JSON.stringify(L)], { type: "application/octet-stream" }));\n'
            '  a.download = `${(L.name || "Untitled show").replace(/[\\\\/:]/g, "-")}.2box.json`;\n'
            '  document.body.appendChild(a); a.click(); a.remove();\n'
            '  setTimeout(() => URL.revokeObjectURL(a.href), 60000);\n'
            '};\n'
            '$("openShowFile").onclick = () => $("showFileInput").click();\n'
            '$("showFileInput").onchange = async e => {\n'
            '  const f = e.target.files[0]; e.target.value = ""; if (!f) return;\n'
            '  let saved; try { saved = JSON.parse(await f.text()); } catch { saved = null; }\n'
            '  if (!saved || !Array.isArray(saved.boxes)) return toast("That isn\'t a SuperSource Builder show file.", true);\n'
            '  if (!await okToReplace(`Opening ${f.name}`)) return;\n'
            '  await useSavedShow(saved);\n'
            '  toast(`<b>Opened</b> ${f.name}. Press Save to keep it on this One.`);\n'
            '};\n', "SS: open a show file", re.S)
    # the toast's Show in Finder has nothing to show from a browser
    t = sub(t, r'function toast\(msg, err, revealPath, action\) \{\n', 'function toast(msg, err, revealPath, action) {\n  revealPath = null;\n', "SS: toast reveal")
    t = sub(t, r"</body>", ALIVE_JS, "SS: heartbeat")
    return t.replace("Choose an image from the Mac", "Choose an image from this computer").replace("Show in Finder", "Download").replace("Downloads + exports/", "exports/").replace("on this Mac", "on this One")


def ss_py(t):
    t = sub(t, r'SIGNUP_URL = .*', 'SIGNUP_URL = ""', "SS: signup address")
    t = sub(t, r'def send_signup\(\):.*?(?=\ndef read_switcher)', 'def send_signup():\n    return\n\n', "SS: send_signup", re.S)
    t = sub(t, r'[ \t]*threading\.Thread\(target=send_signup, daemon=True\)\.start\(\)[^\n]*\n', "", "SS: signup thread", count=2)
    t = sub(t, r'"showBuilder": bool\(show_builder\(\)\)', '"showBuilder": True', "SS: Show Builder is here")
    t = sub(t, r'"bonus": bool\(saved_link\("bonus"\)\)', '"bonus": True', "SS: bonus in meta")
    t = sub(t, r'if self\.path == "/api/bonus/unlock":.*?(?=if self\.path == "/api/profile/read":)',
            'if self.path in ("/api/bonus/unlock", "/api/bonus/set"):\n                return self.send(200, {"bonus": True})\n            ', "SS: bonus routes", re.S)
    t = sub(t, r'if self\.path == "/api/quit":', 'if False:   # the One keeps it running', "SS: quit route")
    t = sub(t, r'if self\.path == "/api/meta":',
            'if self.path.startswith("/api/exportzip?"):\n'
            '                import io, zipfile\n'
            '                q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)\n'
            '                n = slug((q.get("name") or [""])[0])\n'
            '                top = os.path.join(EXPORTS, n)\n'
            '                buf = io.BytesIO()\n'
            '                with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:\n'
            '                    for d, _, fs in os.walk(top):\n'
            '                        for f in fs:\n'
            '                            p = os.path.join(d, f)\n'
            '                            z.write(p, os.path.join(n, os.path.relpath(p, top)))\n'
            '                data = buf.getvalue()\n'
            '                self.send_response(200)\n'
            '                self.send_header("Content-Type", "application/zip")\n'
            '                self.send_header("Content-Disposition", "attachment; filename*=UTF-8\'\'" + urllib.parse.quote(n + ".zip"))\n'
            '                self.send_header("Content-Length", str(len(data)))\n'
            '                self.end_headers()\n'
            '                self.wfile.write(data)\n'
            '                return\n'
            '            if self.path == "/api/meta":', "SS: zip route")
    t = sub(t, r'srv = ThreadingHTTPServer\(\("127\.0\.0\.1", a\.port\), H\)', 'srv = ThreadingHTTPServer(("0.0.0.0", a.port), H)', "SS: bind")
    return stops_when_idle(t, "H", "SS")


def ss_read(t):
    return sub(t, r'NODE_PATHS = \[os\.path\.join\(ROOT, "atem-link", "bin", "node"\),\n\s*"/Applications/Companion\.app/Contents/Resources/node-runtimes/node22/bin/node"\]',
               'NODE_PATHS = ["/opt/companion/node-runtimes/node22/bin/node", "/opt/companion/node-runtimes/main/bin/node"]', "SS: node path")


def edit(path, fn):
    t = open(path, encoding="utf-8").read()
    open(path, "w", encoding="utf-8").write(fn(t))


def font_file(name):
    for d in (os.path.expanduser("~/Library/Fonts"), "/Library/Fonts"):
        if os.path.isfile(os.path.join(d, name)):
            return os.path.join(d, name)
    sys.exit(f"STOP: font {name} isn't on this Mac")


def stage_companion(root, from_tree):
    dest = os.path.join(root, "Companion")
    paths = ["tools/show-builder/show-builder.py", "tools/show-builder/companion_link.py", "tools/show-builder/index.html",
             "tools/show-builder/app-icon.png", "tools/show-builder/donor-atem-aux.json", "tools/show-builder/donor-atem-ssrc.json",
             "tools/show-builder/donor-atem-page8.companionconfig", "tools/atem-me-pages.py", "tools/atem-ss-looks-page.py",
             "tools/atem-sources-page.py", "downstage-page-PLUSXL-v2.companionconfig", "button-pack"]
    if from_tree:
        for p in paths:
            s, d = os.path.join(COMPANION, p), os.path.join(dest, p)
            os.makedirs(os.path.dirname(d), exist_ok=True)
            shutil.copytree(s, d) if os.path.isdir(s) else shutil.copy(s, d)
        rev = "working tree"
    else:
        os.makedirs(dest)
        tar = subprocess.run(["git", "-C", COMPANION, "archive", "HEAD", *paths], capture_output=True, check=True).stdout
        with tarfile.open(fileobj=io.BytesIO(tar)) as tf:
            tf.extractall(dest, filter="data")
        rev = subprocess.run(["git", "-C", COMPANION, "rev-parse", "--short", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
    os.makedirs(os.path.join(dest, "fonts"))
    for f in FONTS:
        shutil.copy(font_file(f), os.path.join(dest, "fonts"))
    edit(os.path.join(dest, "tools/show-builder/index.html"), show_html)
    edit(os.path.join(dest, "tools/show-builder/show-builder.py"), show_py)
    edit(os.path.join(dest, "tools/atem-me-pages.py"), me_pages)
    return rev


def stage_ss(root):
    dest = os.path.join(root, "supersource")
    os.makedirs(os.path.join(dest, "twobox"))
    for f in ("server.py", "atem_read.py", "index.html", "app-icon.png"):
        shutil.copy(os.path.join(SS, "twobox", f), os.path.join(dest, "twobox"))
    for f in ("ss_anim.py", "preview_template.html"):
        shutil.copy(os.path.join(SS, f), dest)
    shutil.copytree(os.path.join(SS, "templates"), os.path.join(dest, "seed", "templates"))
    os.makedirs(os.path.join(dest, "fonts"))
    for f in FONTS:
        shutil.copy(font_file(f), os.path.join(dest, "fonts"))
    # the switcher reader, with only the Pi's prebuilt native parts
    shutil.copytree(os.path.join(SS, "atem-link"), os.path.join(dest, "atem-link"),
                    ignore=lambda d, names: [n for n in names if (n == "bin") or (os.path.basename(d) == "prebuilds" and "linux-arm64" not in n)
                                             or n in (".yarn-state.yml", ".cache")])
    edit(os.path.join(dest, "twobox", "index.html"), ss_html)
    edit(os.path.join(dest, "twobox", "server.py"), ss_py)
    edit(os.path.join(dest, "twobox", "atem_read.py"), ss_read)


# What must not be in anything a customer's One serves. MailerLite and the like stay out of the tree entirely.
BANNED = {
    "Companion/tools/show-builder/index.html": ["downstage.systems", "Get a Downstage One", "Get updates", "Coming later", "checkFlyer", "openOnePop", 'showbuilder-opens"', "discussions/new"],
    "Companion/tools/show-builder/show-builder.py": ["one-photo", "~/Documents/Downstage Shows"],
    "supersource/twobox/index.html": ["downstage.systems", "Unlock the bonus", "Enter your email", "let BONUS = false", "api/bonus", "Show in Finder"],
    "supersource/twobox/server.py": ["mailerlite", "saved_link(\"bonus\")", "target=send_signup"],
}


def check(root):
    for rel, words in BANNED.items():
        t = open(os.path.join(root, rel), encoding="utf-8").read()
        for w in words:
            if w in t:
                sys.exit(f"STOP: {rel} still has {w!r}")
    if os.path.exists(os.path.join(root, "Companion/tools/show-builder/one-photo.webp")):
        sys.exit("STOP: the One's sales photo is in the build")
    for d in ("shows", "exports", "profiles"):
        if os.path.exists(os.path.join(root, "supersource", d)):
            sys.exit(f"STOP: client work ({d}/) is in the build")
    for rel in ("Companion/tools/show-builder/show-builder.py", "Companion/tools/show-builder/companion_link.py", "Companion/tools/atem-me-pages.py",
                "Companion/tools/atem-ss-looks-page.py", "Companion/tools/atem-sources-page.py", "supersource/twobox/server.py", "supersource/twobox/atem_read.py", "supersource/ss_anim.py"):
        compile(open(os.path.join(root, rel), encoding="utf-8").read(), rel, "exec")


def pack(root, rev):
    open(os.path.join(root, "BUILD"), "w").write(f"show-builder {rev}\n")
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for d, dirs, files in os.walk(root):
            dirs.sort()
            for f in sorted(files):
                if f in (".DS_Store",) or f.endswith(".pyc"):
                    continue
                p = os.path.join(d, f)
                info = tf.gettarinfo(p, os.path.relpath(p, root))
                info.mtime = 0; info.uid = info.gid = 0; info.uname = info.gname = ""     # the same input packs to the same bytes
                with open(p, "rb") as fh:
                    tf.addfile(info, fh)
    data = gzip.compress(buf.getvalue(), 9, mtime=0)
    open(OUT, "wb").write(data)
    print(f"{OUT}\n  {len(data) / 1e6:.1f} MB, sha {hashlib.sha256(data).hexdigest()[:12]}, Show Builder {rev}")


def main():
    with tempfile.TemporaryDirectory() as tmp:
        rev = stage_companion(tmp, "--companion-tree" in sys.argv)
        stage_ss(tmp)
        check(tmp)
        pack(tmp, rev)


if __name__ == "__main__":
    main()
