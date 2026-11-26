"""Build a separate patched Windows release from the original 0.18.3 ZIP."""

import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import struct
import zipfile


ROOT = Path(__file__).resolve().parent.parent
ORIGINAL = ROOT / "KabanMusic-0.18.3-win.zip"
OUTPUT = ROOT / "KabanMusic-0.18.3-playback-fix-win.zip"


def replace_once(code, old, new):
    if code.count(old) != 1:
        raise ValueError(f"Expected one match for {old!r}")
    return code.replace(old, new, 1)


def patch_main(code):
    start = code.index("async function load(videoId)")
    end = code.index("async function seek(seconds)", start)
    code = code[:start] + r'''var playbackRevision = 0;
var wantsPageAudio = false;
function mutePage(muted) {
  if (engine && !engine.isDestroyed()) engine.webContents.setAudioMuted(muted);
}
async function load(videoId) {
  if (!/^[\w-]{11}$/.test(videoId)) throw new Error("bad video id");
  const revision = ++playbackRevision;
  expectedId = videoId;
  wantsPageAudio = true;
  loadedAt = Date.now();
  stall = { t: -1, since: 0, heals: 0 };
  stalledReport = false;
  startEngine();
  mutePage(true);
  await engineReady;
  if (revision !== playbackRevision) return;
  await exec(VOLUME_JS());
  if (revision !== playbackRevision) return;
  await exec(HOLD_JS(videoId));
  if (revision !== playbackRevision) return;
  const ok = await exec(
    `(()=>{const p=${PLAYER};if(!p||!p.loadVideoById)return false;
      p.setPlaybackQualityRange&&p.setPlaybackQualityRange('tiny','tiny');
      p.loadVideoById(${JSON.stringify(videoId)});
      p.setPlaybackQualityRange&&p.setPlaybackQualityRange('tiny','tiny');return true})()`
  );
  if (revision !== playbackRevision) return;
  if (!ok && engine) {
    engineReady = engine.loadURL(`${MUSIC}/watch?v=${videoId}`).catch(() => {});
    await engineReady;
    if (revision !== playbackRevision) return;
    await exec(VOLUME_JS());
    if (revision !== playbackRevision) return;
    await exec(HOLD_JS(videoId));
  }
  if (revision === playbackRevision) loadedAt = Date.now();
}
async function command(cmd) {
  if (cmd === "play" && !expectedId) return;
  wantsPageAudio = cmd === "play";
  if (!wantsPageAudio) mutePage(true);
  if (cmd === "stop") {
    ++playbackRevision;
    expectedId = null;
  }
  const fn = cmd === "play" ? "playVideo" : cmd === "pause" ? "pauseVideo" : "stopVideo";
  await exec(`(()=>{const p=${PLAYER};p&&p.${fn}&&p.${fn}()})()`);
}
''' + code[end:]
    code = replace_once(code, "  wc.setUserAgent(ytSession.getUserAgent());",
        '''  wc.setAudioMuted(true);
  wc.on("did-start-navigation", (_event, _url, inPlace, mainFrame) => {
    if (mainFrame && !inPlace) wc.setAudioMuted(true);
  });
  wc.setUserAgent(ytSession.getUserAgent());''')
    start = code.index("async function heal(videoId")
    end = code.index("var client = null;", start)
    playback = code[start:end]
    playback = replace_once(playback, "  if (!engine) return;", """  if (!engine || !wantsPageAudio) return;
  const revision = playbackRevision;
  mutePage(true);""")
    playback = replace_once(playback, """  await engineReady;
  await exec(VOLUME_JS());
  await exec(HOLD_JS(videoId));""", """  await engineReady;
  if (revision !== playbackRevision || !wantsPageAudio) return;
  await exec(VOLUME_JS());
  if (revision !== playbackRevision || !wantsPageAudio) return;
  await exec(HOLD_JS(videoId));""")
    playback = replace_once(playback, "    const ok = await exec(", """    if (revision !== playbackRevision || !wantsPageAudio) return;
    const ok = await exec(""")
    playback = replace_once(playback, """  await exec(VOLUME_JS());
  loadedAt = Date.now();
}""", """  if (revision === playbackRevision) loadedAt = Date.now();
}""")
    playback = replace_once(playback, """  polling = true;
  try {""", """  polling = true;
  const revision = playbackRevision;
  const ready = engineReady;
  try {
    if (!wantsPageAudio) {
      await exec(`(()=>{const p=${PLAYER};if(p&&p.getPlayerState?.()===1)p.pauseVideo?.()})()`);
      return;
    }""")
    playback = replace_once(playback, "    if (!s || !expectedId) return;", """    if (revision !== playbackRevision || ready !== engineReady || !wantsPageAudio) return;
    mutePage(!s || s.id !== expectedId || s.ad === true);
    if (!s) return;""")
    playback = replace_once(playback, '      await command("pause");\n      state = 0;',
        '      await command("pause");\n      if (revision !== playbackRevision) return;\n      state = 0;')
    return code[:start] + playback + code[end:]


def patch_renderer(code):
    code = replace_once(code, '''      void api.ytCommand("pause");
      this.engine = "audio";''', '''      await api.ytCommand("stop");
      if (seq !== this.loadSeq) return;
      this.engine = "audio";''')
    code = replace_once(code, '''      if (this.engine === "page") void api.ytCommand("pause");
      clearInterval(this.fadeTimer);''', '''      void api.ytCommand("stop");
      clearInterval(this.fadeTimer);''')
    return replace_once(code, '''      if (this.isYt) void api.ytCommand("pause");
      else this.audio.pause();''', '''      void api.ytCommand("pause");
      this.audio.pause();''')


def entries(tree, prefix=""):
    for name, item in tree.items():
        path = prefix + name
        if "files" in item:
            yield from entries(item["files"], path + "/")
        elif "offset" in item:
            yield path, item


def patch_ui_html(code):
    code = replace_once(code, '      <div class="sidebar-section">Плейлисты</div>',
        '''      <div class="sidebar-section">
        <h2>Плейлисты</h2>
        <button class="icon-btn sidebar-playlist-add" id="btnNewSidebarPlaylist" title="Новый плейлист"><span aria-hidden="true">+</span></button>
      </div>''')
    for button in ("btnOpenExt", "btnDownload"):
        line, = (line for line in code.splitlines(keepends=True) if f'id="{button}"' in line)
        code = replace_once(code, line, "")
    more, = (line for line in code.splitlines(keepends=True) if 'id="btnMore"' in line)
    code = replace_once(code, more, "")
    similar, = (line for line in code.splitlines(keepends=True) if 'id="btnSimilar"' in line)
    return replace_once(code, similar, '''        <button class="icon-btn np-ext" id="btnTrackRadio" title="Радио по этому треку" hidden><i data-icon="radio"></i></button>
        <button class="icon-btn np-ext" id="btnMore" title="Действия с треком" aria-haspopup="true" hidden><i data-icon="more"></i></button>
''')


def patch_ui_css(code):
    start = code.index(".sidebar-section {")
    end = code.index("/* main */", start)
    code = code[:start] + """.sidebar-section { display: flex; align-items: center; justify-content: space-between; gap: 8px; margin: 10px 0 2px; padding: 5px 4px 0 10px; border-top: 1px solid var(--line); }
.sidebar-section h2 { margin: 0; font-size: 15px; font-weight: 650; color: var(--text); }
.sidebar-playlist-add.icon-btn { width: 28px; height: 28px; font-size: 21px; line-height: 1; flex: none; }
.sidebar-playlists { overflow-y: auto; overscroll-behavior: contain; min-height: 0; flex: 1; }
.sidebar-playlist-group + .sidebar-playlist-group { margin-top: 6px; }
.sidebar-playlist-group summary { display: flex; align-items: center; gap: 6px; min-height: 28px; padding: 0 8px; border-radius: 6px; font-size: 12px; font-weight: 600; color: var(--muted); cursor: pointer; }
.sidebar-playlist-group summary::-webkit-details-marker { display: none; }
.sidebar-playlist-group summary svg { width: 14px; height: 14px; flex: none; transform: rotate(-90deg); }
.sidebar-playlist-group[open] summary svg { transform: none; }
.sidebar-playlist-group summary:hover, .sidebar-playlist-row:hover { color: var(--text); background: var(--panel); }
.sidebar-playlist-row { display: flex; align-items: center; gap: 8px; width: 100%; min-height: 32px; padding: 3px 8px 3px 18px; border-radius: 6px; font-size: 13px; color: var(--artist-text); text-align: left; }
.sidebar-playlist-row svg { width: 16px; height: 16px; flex: none; }
.sidebar-playlist-row span { min-width: 0; overflow: hidden; white-space: nowrap; text-overflow: ellipsis; }
.sidebar-playlist-row[aria-current="page"] { background: var(--panel-2); color: var(--text); box-shadow: inset 2px 0 var(--accent); }
.sidebar-playlist-row[aria-current="page"] svg { color: var(--accent); }
.sidebar-playlist-add:focus-visible, .sidebar-playlist-group summary:focus-visible, .sidebar-playlist-row:focus-visible { outline: 1px solid var(--accent); outline-offset: -2px; }
.sidebar-playlists-empty { margin: 3px 10px; font-size: 12px; color: var(--muted); }

""" + code[end:]
    code = replace_once(code, ".sidebar-sep { height: 1px; background: var(--line); margin: 6px 12px; }\n", "")
    start = code.index("/* now playing: keep the title readable")
    end = code.index("/* ---- 0.15:", start)
    code = code[:start] + code[end:]
    code = replace_once(code, ".np-text { min-width: 0; }", ".np-text { flex: 1; min-width: 0; }")
    code = replace_once(code, ".np { display: flex; align-items: center; gap: 12px;",
        ".np { display: flex; align-items: center; gap: 6px;")
    code = replace_once(code, ".np-text { flex: 1; min-width: 0; }",
        ".np-text { flex: 1; min-width: 0; margin-inline: 6px; }")
    code = replace_once(code, ".np-like { opacity: .8; }",
        "#npLike { display: flex; flex: none; }\n#npLike:empty { display: none; }\n.np-like { opacity: .8; }")
    code = replace_once(code, ".menu-sep {", """.menu.player-track-menu { max-height: calc(100vh - 16px); overflow-y: auto; overscroll-behavior: contain; }
.player-track-menu .menu-head { color: var(--muted); }
.player-track-menu .menu-sep { margin-block: 6px; }
.menu-sep {""")
    return replace_once(code, ".modal-input:focus { border-color: var(--accent-2); }", """.modal-input:focus { border-color: var(--accent-2); }
.playlist-picker { width: min(440px, calc(100vw - 32px)); color: var(--text); }
.playlist-picker::backdrop { background: #000a; }
.playlist-picker select { color: var(--text); }""")


def group_track_menu(code):
    start = code.index("  function trackMenu(")
    end = code.index("  function mineItems(", start)
    menu = code[start:end]
    menu = replace_once(menu, "    openMenu(e.clientX, e.clientY, [", "    const library = [")
    boundaries = [
        ("      ...originalOf(t2)", "replacement"),
        (r'      [t("\u0418\u0433\u0440\u0430\u0442\u044C \u0441\u043B', "queue"),
        (r'      [t("\u041F\u043E\u0445\u043E\u0436\u0438\u0435 ', "discovery"),
        (r'      [t("\u0420\u0430\u0434\u0438\u043E ', "radio"),
        (r'      [t("\u041F\u0435\u0440\u0435\u0439\u0442\u0438 ', "artist"),
        ('      ...t2.source === "yt"', "files"),
        ("      ...t2.albumId", "album"),
    ]
    for marker, name in boundaries:
        menu = replace_once(menu, marker, f"    ];\n    const {name} = [\n{marker}")
    menu = replace_once(menu, "    ]);\n  }", r'''    ];
    const items = ctx?.nowPlaying ? [
      t("\u041E\u0447\u0435\u0440\u0435\u0434\u044C"), ...queue,
      null, t("\u0411\u0438\u0431\u043B\u0438\u043E\u0442\u0435\u043A\u0430"), ...library,
      null, t("О треке"), ...discovery, ...artist, ...album,
      null, t("Файл и источник"), ...files, ...replacement
    ] : [...library, ...replacement, ...queue, ...discovery, ...radio, ...artist, ...files, ...album];
    openMenu(e.clientX, e.clientY, items, ctx?.nowPlaying);
  }''')
    return code[:start] + menu + code[end:]


def patch_sidebar_renderer(code):
    start = code.index("  async function loadSidebarPlaylists()")
    end = code.index("  var fmtT", start)
    code = code[:start] + r'''  var sidebarPlaylistLoad = 0;
  function vkPlaylistKey(playlist) {
    return playlist === "my" ? "my" : String(playlist.ownerId) + ":" + playlist.id;
  }
  function syncSidebarPlaylistSelection() {
    const source = currentView.vkPlaylist ? "vk" : currentView.myPlaylist != null ? "mine" : "yt";
    const selected = source === "vk" ? vkPlaylistKey(currentView.vkPlaylist) : currentView.myPlaylist ?? currentView.playlist?.id;
    $("sidebarPlaylists").querySelectorAll(".sidebar-playlist-row").forEach((row) => {
      const active = selected != null && row.dataset.source === source && row.dataset.playlistId === String(selected);
      if (active) row.setAttribute("aria-current", "page");
      else row.removeAttribute("aria-current");
    });
  }
  async function loadSidebarPlaylists() {
    const request = ++sidebarPlaylistLoad;
    const root = $("sidebarPlaylists");
    const status = api.status().catch(() => null);
    const [mine, yts, vk] = await Promise.all([
      api.myPlaylists().catch(() => []),
      status.then((st) => st?.google?.loggedIn ? api.playlists() : []).catch(() => []),
      status.then((st) => st?.vk?.loggedIn ? api.vkPlaylists().catch(() => []) : null)
    ]);
    if (request !== sidebarPlaylistLoad) return;
    const scroll = root.scrollTop;
    const focused = document.activeElement;
    const row = (source, id, label, view, glyph) => {
      const button = h("button", { class: "sidebar-playlist-row", title: label, onclick: () => go(view) },
        icon(glyph), h("span", {}, label));
      button.dataset.source = source;
      button.dataset.playlistId = id;
      if (focused?.dataset.source === source && focused.dataset.playlistId === String(id)) focusNext = button;
      return button;
    };
    let focusNext;
    const group = (key, label, rows) => {
      const previous = root.querySelector("#" + key);
      const summary = h("summary", {}, icon("chevron"), label);
      if (previous?.firstElementChild === focused) focusNext = summary;
      return h("details", { id: key, class: "sidebar-playlist-group", open: previous?.open ?? true }, summary, ...rows);
    };
    const groups = [];
    if (mine.length) groups.push(group("sidebarMine", t("\u041C\u043E\u0438 \u043F\u043B\u0435\u0439\u043B\u0438\u0441\u0442\u044B"),
      mine.map((p) => row("mine", p.id, plName(p), { myPlaylist: p.id }, p.system ? "download" : "queue"))));
    if (yts.length) groups.push(group("sidebarYoutube", "YouTube Music",
      yts.map((p) => row("yt", p.id, p.title, { playlist: p }, "queue"))));
    if (vk) groups.push(group("sidebarVk", t("VK Музыка"), [
      row("vk", "my", t("\u041C\u043E\u044F \u043C\u0443\u0437\u044B\u043A\u0430"), { vkPlaylist: "my" }, "library"),
      ...vk.map((p) => row("vk", vkPlaylistKey(p), p.title, { vkPlaylist: p }, "queue"))
    ]));
    root.replaceChildren(...(groups.length ? groups :
      [h("p", { class: "sidebar-playlists-empty" }, t("\u041F\u043B\u0435\u0439\u043B\u0438\u0441\u0442\u043E\u0432 \u043D\u0435\u0442"))]));
    syncSidebarPlaylistSelection();
    root.scrollTop = scroll;
    focusNext?.focus({ preventScroll: true });
  }
''' + code[end:]
    code = replace_once(code, '    currentView = v;\n    document.querySelectorAll(".nav")',
        '    currentView = v;\n    syncSidebarPlaylistSelection();\n    document.querySelectorAll(".nav")')
    code = replace_once(code, '    if ("myPlaylist" in v) return viewMyPlaylist(v.myPlaylist);',
        '''    if ("myPlaylist" in v) return viewMyPlaylist(v.myPlaylist);
    if ("vkPlaylist" in v) {
      const body = h("div", {}, loading());
      main().replaceChildren(body);
      return libVk(body, v.vkPlaylist, true);
    }''')
    code = replace_once(code, '    $("btnBack").onclick = back;',
        '    $("btnBack").onclick = back;\n    $("btnNewSidebarPlaylist").onclick = () => newMyPlaylist();')
    code = replace_once(code, '    renderQueue();\n    void go(currentView, false);\n  }\n  async function init()',
        '    renderQueue();\n    void loadSidebarPlaylists();\n    void go(currentView, false);\n  }\n  async function init()')
    code = replace_once(code, '      if (e.code === "Space" && !typing) {\n        e.preventDefault();',
        '''      if (e.code === "Space" && !typing) {
        if (e.target.closest("#btnNewSidebarPlaylist, .sidebar-playlist-group summary, .sidebar-playlist-row")) return;
        e.preventDefault();''')
    return replace_once(code, 'if (currentView === "settings" || currentView === "library") go(currentView);',
        'if (currentView === "settings" || currentView === "library" || currentView.vkPlaylist) go(currentView, false);')


def patch_vk_transfer(code):
    start = code.index("  async function libVk(body)")
    end = code.index("  var transferCb", start)
    view = code[start:end]
    view = replace_once(view, "async function libVk(body)", "async function libVk(body, selection = vkSel, fromSidebar = false)")
    view = replace_once(view, "    if (vkSel) {\n      const sel = vkSel;", "    if (selection) {\n      const sel = selection;")
    view = replace_once(view, '          h("button", { class: "link back-link",',
        '          !fromSidebar && h("button", { class: "link back-link",')
    old_label = r't("\u041F\u0435\u0440\u0435\u043D\u0435\u0441\u0442\u0438 \u0432 KabanMusic")'
    view = replace_once(view, "    const transferBtn = (pl) => {",
        '    const transferBtn = (pl) => {\n      const label = t("Перенести в мои плейлисты");')
    if view.count(old_label) != 2:
        raise ValueError("Expected the VK import label in the button and error handler")
    view = view.replace(old_label, "label")
    view = replace_once(view, "      b.onclick = async () => {\n        b.disabled = true;",
        '''      b.onclick = async () => {
        if (transferCb) {
          toast(t("Дождись завершения текущего переноса."));
          return;
        }
        b.disabled = true;''')
    view = replace_once(view, '        transferCb = off;\n        try {',
        '        transferCb = off;\n        off({ stage: "load", done: 0 });\n        try {')
    view = replace_once(view, "          go({ myPlaylist: r.playlistId });", "          if (body.isConnected) go({ myPlaylist: r.playlistId });")
    view = replace_once(view, '''          toast(errMsg(e), true);
          b.disabled = false;
          b.textContent = label;
        }
        transferCb = null;''',
        '''          toast(errMsg(e), true);
        } finally {
          transferCb = null;
          b.disabled = false;
          b.textContent = label;
        }''')
    code = code[:start] + view + code[end:]
    return replace_once(code, '    "Действия с треком": "Track actions",',
        '''    "Действия с треком": "Track actions",
    "VK Музыка": "VK Music",
    "Перенести в мои плейлисты": "Import to My playlists",
    "Дождись завершения текущего переноса.": "Wait for the current import to finish.",''')


def patch_youtube_import_main(code):
    code = replace_once(code, '''  if (next && continuations.has(next)) {
    page = await continuations.get(next).getContinuation();''',
        '''  if (next) {
    const previous = continuations.get(next);
    if (!previous) throw new Error(t("Список устарел. Открой плейлист заново."));
    page = await previous.getContinuation();''')
    code = replace_once(code, '''  const raw = page.items ?? page.contents ?? [];
  return {
    items: raw.map(fromListItem).filter((t2) => !!t2),
    next: remember(page)
  };
}''', '''  return { items: youtubePlaylistItems(page), next: remember(page) };
}
function youtubePlaylistItems(page) {
  return (page.items ?? page.contents ?? []).map(fromListItem).filter(Boolean);
}
async function importYoutubePlaylist(id, name) {
  let page = await (await yt()).music.getPlaylist(id.replace(/^VL/, ""));
  const tracks = [];
  // The library's decoder keeps continuation tokens private; bound requests even if a server repeats a page.
  for (let pages = 0; pages < MAX_TRACKS; pages++) {
    tracks.push(...youtubePlaylistItems(page));
    if (tracks.length > MAX_TRACKS) break;
    if (!page.has_continuation) return { playlistId: create(name, tracks), total: tracks.length };
    page = await page.getContinuation();
  }
  throw new Error(t("Плейлист слишком большой для переноса. Максимум: {0} треков.", MAX_TRACKS));
}''')
    code = replace_once(code, '  handle("playlist", (id, next) => playlist(str(id, 200), optStr(next)));',
        '''  handle("playlist", (id, next) => playlist(str(id, 200), optStr(next)));
  handle("importYoutubePlaylist", (id, name) => importYoutubePlaylist(str(id, 200), str(name, 150)));''')
    code = replace_once(code, '''  load6().push(p);
  save3();
  return p.id;''', '''  const playlists = load6();
  playlists.push(p);
  try {
    save3();
  } catch (error) {
    playlists.pop();
    throw error;
  }
  return p.id;''')
    return replace_once(code, 'var EN = {', '''var EN = {
  "Список устарел. Открой плейлист заново.": "This list has expired. Reopen the playlist.",
  "Плейлист слишком большой для переноса. Максимум: {0} треков.": "This playlist is too large to import. Maximum: {0} tracks.",''')


def patch_youtube_import_renderer(code):
    code = replace_once(code, '    "Перенести в мои плейлисты": "Import to My playlists",',
        '''    "Перенести в мои плейлисты": "Import to My playlists",
    "Переношу из YouTube Music…": "Importing from YouTube Music…",
    "Перенесено треков: {0}": "Tracks imported: {0}",''')
    code = replace_once(code, '  async function pagedTracks(root, fetchPage, label, playlistId) {',
        '  async function pagedTracks(root, fetchPage, label, playlistId, importButton) {')
    code = replace_once(code, '''    const load = async () => {
      more.disabled = true;
      const page = await fetchPage(next);
      all.push(...page.items);
      next = page.next;
      listWrap.replaceChildren(trackList(all, label, void 0, playlistId));
      more.hidden = !next;
      more.disabled = false;
      markPlaying();
    };
    more.onclick = () => load().catch((e) => toast(errMsg(e), true));''',
        '''    let loadingPage;
    const load = () => loadingPage ??= (async () => {
      more.disabled = true;
      try {
        const page = await fetchPage(next);
        all.push(...page.items);
        next = page.next;
        listWrap.replaceChildren(trackList(all, label, void 0, playlistId));
        more.hidden = !next;
        markPlaying();
      } finally {
        more.disabled = false;
        loadingPage = null;
      }
    })();
    more.onclick = () => {
      if (!loadingAll) void load().catch((e) => toast(errMsg(e), true));
    };''')
    code = replace_once(code, '''      if (!next) return;
      const label0 = btn.innerHTML;
      btn.disabled = true;''', '''      const label0 = btn.innerHTML;
      btn.disabled = true;
      more.disabled = true;''')
    code = replace_once(code, '''        while (next && all.length < 5e3) {''',
        '''        await loadingPage;
        more.disabled = true;
        while (next && all.length < 5e3) {''')
    code = replace_once(code, '''        btn.disabled = false;
        loadingAll = null;''', '''        btn.disabled = false;
        more.disabled = false;
        loadingAll = null;''')
    code = replace_once(code, '    actions.append(playBtn, shuffleBtn);\n    root.replaceChildren',
        '    actions.append(playBtn, shuffleBtn);\n    if (importButton) actions.append(importButton);\n    root.replaceChildren')
    code = replace_once(code, '  async function viewPlaylist(p) {', r'''  var youtubeImports = new Map();
  function youtubeImportButton(p, body) {
    const key = p.id.replace(/^VL/, "");
    const title = t("Перенести в мои плейлисты");
    const button = h("button", { class: "btn" }, title);
    const showPending = (task) => {
      button.disabled = true;
      button.textContent = t("Переношу из YouTube Music…");
      void task.then(() => {
        button.disabled = false;
        button.textContent = title;
      });
    };
    const pending = youtubeImports.get(key);
    if (pending) showPending(pending);
    button.onclick = () => {
      if (youtubeImports.has(key)) return;
      const task = (async () => {
        try {
          const name = (p.title || t("\u041F\u043B\u0435\u0439\u043B\u0438\u0441\u0442")).slice(0, 150);
          const result = await api.importYoutubePlaylist(p.id, name);
          void loadSidebarPlaylists();
          toast(t("Перенесено треков: {0}", result.total));
          if (body.isConnected) go({ myPlaylist: result.playlistId });
        } catch (error) {
          toast(errMsg(error), true);
        } finally {
          youtubeImports.delete(key);
        }
      })();
      youtubeImports.set(key, task);
      showPending(task);
    };
    return button;
  }
  async function viewPlaylist(p) {''')
    code = replace_once(code, 'await pagedTracks(body, (next) => api.playlist(p.id, next), p.title, p.id);',
        'await pagedTracks(body, (next) => api.playlist(p.id, next), p.title, p.id, youtubeImportButton(p, body));')
    code = replace_once(code, '#btnNewSidebarPlaylist, .sidebar-playlist-group summary, .sidebar-playlist-row',
        '.sidebar button, .sidebar-playlist-group summary, .np button, #menu button')
    return replace_once(code, '      const typing = e.target.matches("input, textarea");',
        '''      const menu = $("menu");
      if (e.key === "Escape" && !menu.hidden) {
        menu.hidden = true;
        if (menu.classList.contains("player-track-menu")) $("btnMore").focus();
        return;
      }
      const typing = e.target.matches("input, textarea");''')


def patch_windows_integration(code):
    code = replace_once(code, 'import_electron15.app.setAppUserModelId("app.kabanmusic");',
        '''var WINDOWS_APP_ID = "app.kabanmusic";
import_electron15.app.setAppUserModelId(WINDOWS_APP_ID);''')
    code = replace_once(code,
        '    ...ACRYLIC ? { backgroundMaterial: "acrylic", backgroundColor: "#00000000" } : { backgroundColor: "#000000" },',
        '''    ...ACRYLIC ? { backgroundMaterial: "acrylic", backgroundColor: "#00000000" } : { backgroundColor: "#000000" },
    ...process.platform === "win32" ? { accentColor: "#161616" } : {},''')
    code = replace_once(code, '''function mediaKey(k) {
  send3("mediaKey", k);
}''', '''var windowsMediaKeys = [
  ["MediaPlayPause", "playpause"],
  ["MediaNextTrack", "next"],
  ["MediaPreviousTrack", "prev"],
  ["MediaStop", "stop"]
];
var windowsMediaKeysActive = false;
var registeredWindowsMediaKeys = new Set();
function syncWindowsMediaKeys(active) {
  if (process.platform !== "win32" || windowsMediaKeysActive === active) return;
  windowsMediaKeysActive = active;
  if (!active) {
    for (const key of registeredWindowsMediaKeys) import_electron15.globalShortcut.unregister(key);
    registeredWindowsMediaKeys.clear();
    return;
  }
  for (const [key, action] of windowsMediaKeys) {
    try {
      if (import_electron15.globalShortcut.register(key, () => mediaKey(action))) registeredWindowsMediaKeys.add(key);
      else console.warn("[media keys] Windows could not register " + key);
    } catch (error) {
      console.warn("[media keys] " + key + ": " + error.message);
    }
  }
}
function mediaKey(k) {
  if (k === "stop") syncWindowsMediaKeys(false);
  send3("mediaKey", k);
}
function ensureStartMenuShortcut() {
  if (process.platform !== "win32" || !import_electron15.app.isPackaged) return;
  const path = import_node_path16;
  const fs = import_node_fs14;
  const { app, shell } = import_electron15;
  const folder = path.join(app.getPath("appData"), "Microsoft", "Windows", "Start Menu", "Programs");
  const shortcut = path.join(folder, "KabanMusic.lnk");
  try {
    const exists = fs.existsSync(shortcut);
    if (exists) {
      const existing = shell.readShortcutLink(shortcut);
      const sameTarget = existing.target?.toLowerCase() === process.execPath.toLowerCase();
      if (sameTarget && existing.appUserModelId === WINDOWS_APP_ID) return;
      if (!sameTarget && (existing.appUserModelId !== WINDOWS_APP_ID || fs.existsSync(existing.target))) return;
    }
    fs.mkdirSync(folder, { recursive: true });
    const ok = shell.writeShortcutLink(shortcut, exists ? "update" : "create", {
      target: process.execPath,
      cwd: path.dirname(process.execPath),
      description: "KabanMusic",
      appUserModelId: WINDOWS_APP_ID
    });
    if (!ok) console.warn("[start menu] could not write KabanMusic.lnk");
  } catch (error) {
    console.warn("[start menu] " + error.message);
  }
}''')
    code = replace_once(code, '''    } : null;
    pushMini();
    updateDiscord();
    if (win) win.setTitle''', '''    } : null;
    if (!nowPlaying2 || nowPlaying2.playing) syncWindowsMediaKeys(!!nowPlaying2);
    pushMini();
    updateDiscord();
    if (win) win.setTitle''')
    code = replace_once(code, '  win.webContents.once("did-finish-load", () => {',
        '''  win.webContents.on("render-process-gone", () => syncWindowsMediaKeys(false));
  win.webContents.once("did-finish-load", () => {''')
    code = replace_once(code, '  setupShell();\n  applyAutostart();',
        '  setupShell();\n  ensureStartMenuShortcut();\n  applyAutostart();')
    return replace_once(code, 'a === "previoustrack" ? "prev" : a === "stop" ? "stop" : "playpause"',
        'a === "previoustrack" ? "prev" : a')


def patch_windows_media_renderer(code):
    code = replace_once(code, '''      else if (k === "prev") player.prev();
      else player.stop();''', '''      else if (k === "prev") player.prev();
      else if (k === "play") player.play();
      else if (k === "pause") player.pause();
      else if (k === "stop") player.stop();''')
    return replace_once(code, '''      ["pause", () => player.pause()],
      ["nexttrack",''', '''      ["pause", () => player.pause()],
      ["stop", () => player.stop()],
      ["nexttrack",''')


def patch_playlist_merge_main(code):
    code = replace_once(code, 'async function importYoutubePlaylist(id, name) {',
        'async function readYoutubePlaylistTracks(id) {')
    code = replace_once(code, 'if (!page.has_continuation) return { playlistId: create(name, tracks), total: tracks.length };',
        'if (!page.has_continuation) return tracks;')
    code = replace_once(code, 'async function likedSongs(next) {', '''async function importYoutubePlaylist(id, name) {
  const tracks = await readYoutubePlaylistTracks(id);
  return { playlistId: create(name, tracks), total: tracks.length };
}
async function likedSongs(next) {''')
    code = replace_once(code, 'function removeAt(id, index) {', '''function mergeExternalTracks(id, tracks) {
  const p = get(id);
  if (p.system) throw new Error("system playlist");
  const key = (track) => track.source + ":" + track.id;
  const known = new Set(p.tracks.map(key));
  const additions = [];
  for (const track of tracks) {
    const identity = key(track);
    if (known.has(identity)) continue;
    known.add(identity);
    additions.push(strip(track));
  }
  if (p.tracks.length + additions.length > MAX_TRACKS) {
    throw new Error(t("В плейлисте может быть не больше {0} треков.", MAX_TRACKS));
  }
  if (additions.length) {
    const previous = p.tracks;
    const updated = p.updated;
    p.tracks = [...previous, ...additions];
    p.updated = Date.now();
    try {
      save3();
    } catch (error) {
      p.tracks = previous;
      p.updated = updated;
      throw error;
    }
  }
  return { added: additions.length, skipped: tracks.length - additions.length };
}
function removeAt(id, index) {''')
    code = replace_once(code, '  handle("vkPlaylists", () => playlists2());', '''  handle("mergeExternalPlaylist", async (id, source) => {
    const targetId = str(id, 40);
    if (get(targetId).system) throw new Error("system playlist");
    if (source?.service === "yt") return mergeExternalTracks(targetId, await readYoutubePlaylistTracks(str(source.id, 200)));
    if (source?.service === "vk") {
      const selected = source.playlist === null ? null : vkPl(source.playlist);
      if (source.playlist !== null && (!selected?.id || !selected.ownerId)) throw new Error("bad VK playlist");
      const tracks = await allTracks(selected, (_loaded, total) => {
        if (total > MAX_TRACKS) throw new Error(t("Плейлист слишком большой для переноса. Максимум: {0} треков.", MAX_TRACKS));
      });
      return mergeExternalTracks(targetId, tracks);
    }
    throw new Error("bad playlist source");
  });
  handle("vkPlaylists", () => playlists2());''')
    return replace_once(code, 'var EN = {', '''var EN = {
  "В плейлисте может быть не больше {0} треков.": "A playlist can contain up to {0} tracks.",''')


def patch_playlist_merge_renderer(code):
    code = replace_once(code, '    "Перенесено треков: {0}": "Tracks imported: {0}",',
        '''    "Перенесено треков: {0}": "Tracks imported: {0}",
    "Объединить с…": "Merge with…",
    "Перенести в…": "Add to…",
    "Выбери плейлист": "Choose a playlist",
    "Выбери плейлист YouTube Music или VK": "Choose a YouTube Music or VK playlist",
    "Выбери свой плейлист": "Choose one of your playlists",
    "Нет доступных плейлистов.": "No playlists available.",
    "Создай свой плейлист, чтобы добавить в него треки.": "Create a playlist to add tracks to it.",
    "Добавлено: {0}. Уже в списке или повторяются: {1}.": "Added: {0}. Already in the playlist or repeated: {1}.",
    "Добавляю треки…": "Adding tracks…",''')
    code = replace_once(code, '  async function viewMyPlaylist(id) {', r'''  async function externalPlaylistChoices() {
    const status = await api.status();
    const results = await Promise.allSettled([
      status.google?.loggedIn ? api.playlists() : [],
      status.vk?.loggedIn ? api.vkPlaylists() : []
    ]);
    const [yt, vk] = results;
    return {
      groups: [
        { label: "YouTube Music", entries: (yt.value ?? []).map((p) => ({ title: p.title, value: { service: "yt", id: p.id } })) },
        { label: t("VK Музыка"), entries: (vk.value ?? []).map((p) => ({ title: p.title, value: { service: "vk", playlist: p } })) }
      ],
      errors: results.filter((r) => r.status === "rejected").map((r) => errMsg(r.reason))
    };
  }
  async function ownPlaylistChoices() {
    const playlists = await api.myPlaylists();
    return { groups: [{ label: "", entries: playlists.filter((p) => !p.system)
      .map((p) => ({ title: plName(p), value: p.id })) }], errors: [] };
  }
  function choosePlaylist(title, choices, emptyText) {
    return new Promise((resolve) => {
      const values = [];
      let selected;
      const prompt = t("Выбери плейлист");
      const select = h("select", { class: "modal-input", required: true, ariaLabel: prompt },
        h("option", { value: "", disabled: true, selected: true }, prompt));
      const status = h("p", { class: "muted", role: "status" }, t("\u0417\u0430\u0433\u0440\u0443\u0436\u0430\u044E\u2026"));
      const ok = h("button", { class: "btn primary", type: "submit", disabled: true },
        t("\u0413\u043E\u0442\u043E\u0432\u043E"));
      const cancel = h("button", { class: "btn", type: "button", onclick: () => dialog.close() },
        t("\u041E\u0442\u043C\u0435\u043D\u0430"));
      const form = h("form", { onsubmit: (event) => {
        event.preventDefault();
        if (select.value === "") return;
        selected = values[Number(select.value)];
        dialog.close();
      } }, status, select, h("div", { class: "actions" }, ok, cancel));
      const dialog = h("dialog", { class: "modal playlist-picker", ariaLabel: title }, h("h3", {}, title), form);
      select.disabled = true;
      select.onchange = () => ok.disabled = select.value === "";
      dialog.onclose = () => {
        dialog.remove();
        resolve(selected);
      };
      document.body.append(dialog);
      dialog.showModal();
      void choices().then(({ groups, errors }) => {
        if (!dialog.open) return;
        for (const { label, entries } of groups) {
          if (!entries.length) continue;
          const options = entries.map(({ title: name, value }) => {
            const index = values.push(value) - 1;
            return h("option", { value: String(index) }, name);
          });
          if (label) select.append(h("optgroup", { label }, ...options));
          else select.append(...options);
        }
        select.disabled = !values.length;
        status.textContent = errors.join(" · ") || (values.length ? "" : emptyText);
        status.hidden = !status.textContent;
        if (values.length && document.activeElement === cancel) select.focus();
      }).catch((error) => {
        if (dialog.open) status.textContent = errMsg(error);
      });
    });
  }
  function playlistMergeButton({ target, source }) {
    const fromOwn = target != null;
    const label = t(fromOwn ? "Объединить с…" : "Перенести в…");
    const button = h("button", { class: "btn" }, label);
    button.onclick = async () => {
      if (button.disabled) return;
      button.disabled = true;
      try {
        const selection = await choosePlaylist(
          t(fromOwn ? "Выбери плейлист YouTube Music или VK" : "Выбери свой плейлист"),
          fromOwn ? externalPlaylistChoices : ownPlaylistChoices,
          t(fromOwn ? "Нет доступных плейлистов." : "Создай свой плейлист, чтобы добавить в него треки.")
        );
        if (selection == null) return;
        button.textContent = t("Добавляю треки…");
        const id = fromOwn ? target : selection;
        const result = await api.mergeExternalPlaylist(id, fromOwn ? selection : source);
        myPlaylistsChanged(id);
        toast(t("Добавлено: {0}. Уже в списке или повторяются: {1}.", result.added, result.skipped));
      } catch (error) {
        toast(errMsg(error), true);
      } finally {
        button.disabled = false;
        button.textContent = label;
      }
    };
    return button;
  }
  async function viewMyPlaylist(id) {''')
    code = replace_once(code, '    let extra = [renameBtn, delBtn];',
        '    let extra = [playlistMergeButton({ target: id }), renameBtn, delBtn];')
    code = replace_once(code, 'staticTracks(tracks, title, void 0, transferBtn(sel === "my" ? null : sel))',
        '''staticTracks(tracks, title, void 0,
            playlistMergeButton({ source: { service: "vk", playlist: sel === "my" ? null : sel } }),
            transferBtn(sel === "my" ? null : sel))''')
    code = replace_once(code, 'async function pagedTracks(root, fetchPage, label, playlistId, importButton)',
        'async function pagedTracks(root, fetchPage, label, playlistId, ...playlistActions)')
    code = replace_once(code, '''    actions.append(playBtn, shuffleBtn);
    if (importButton) actions.append(importButton);''',
        '    actions.append(playBtn, shuffleBtn, ...playlistActions);')
    code = replace_once(code, 'await pagedTracks(body, (next) => api.playlist(p.id, next), p.title, p.id, youtubeImportButton(p, body));',
        '''await pagedTracks(body, (next) => api.playlist(p.id, next), p.title, p.id,
        playlistMergeButton({ source: { service: "yt", id: p.id } }), youtubeImportButton(p, body));''')
    return replace_once(code, '    document.addEventListener("keydown", (e) => {\n      if (e.altKey',
        '    document.addEventListener("keydown", (e) => {\n      if (e.target.closest("dialog.playlist-picker")) return;\n      if (e.altKey')


def patch_ui_renderer(code):
    for button in ("btnOpenExt", "btnDownload"):
        lines = [line for line in code.splitlines(keepends=True) if f'$("{button}")' in line]
        if len(lines) != 2:
            raise ValueError(f"Expected a visibility rule and click handler for {button}")
        for line in lines:
            code = replace_once(code, line, "")
    code = replace_once(code, r'''() => openExternal(t2)], [t("\u0421\u043A\u0430\u0447\u0430\u0442\u044C \u0444\u0430\u0439\u043B\u043E\u043C"), () => downloadTrack(t2)]] : [],''',
        r'''() => openExternal(t2)]] : [],
      ...(ctx?.nowPlaying || (t2.source === "yt" && t2.id)) ? [[t("\u0421\u043A\u0430\u0447\u0430\u0442\u044C \u0444\u0430\u0439\u043B\u043E\u043C"), () => downloadTrack(t2)]] : [],''')
    code = replace_once(code, 'clientY: r.top }), player.current);',
        'clientY: r.top }), player.current, { nowPlaying: true });')
    code = replace_once(code, '$("btnSimilar").hidden = !t2;',
        '$("btnTrackRadio").hidden = !t2;\n      $("btnMore").hidden = !t2;')
    code = replace_once(code, '$("btnSimilar").onclick = () => player.current && go({ similar: player.current });',
        '$("btnTrackRadio").onclick = () => player.current && player.startRadio(player.current);')
    code = replace_once(code, 'function openMenu(x, y, items) {\n    const m = $("menu");',
        'function openMenu(x, y, items, playerTrack = false) {\n    const m = $("menu");\n    m.classList.toggle("player-track-menu", playerTrack);')
    code = replace_once(code, '    m.hidden = false;\n    const r = m.getBoundingClientRect();',
        '    m.hidden = false;\n    m.scrollTop = 0;\n    const r = m.getBoundingClientRect();')
    code = replace_once(code, 'Math.min(y, innerHeight - r.height - 8)',
        'Math.max(8, Math.min(y, innerHeight - r.height - 8))')
    code = replace_once(code, r'    "\u0411\u0438\u0431\u043B\u0438\u043E\u0442\u0435\u043A\u0430": "Library",',
        r'''    "\u0411\u0438\u0431\u043B\u0438\u043E\u0442\u0435\u043A\u0430": "Library",
    "О треке": "About the track",
    "Файл и источник": "File and source",
    "Действия с треком": "Track actions",''')
    code = patch_vk_transfer(patch_sidebar_renderer(group_track_menu(code)))
    code = patch_playlist_merge_renderer(patch_windows_media_renderer(patch_youtube_import_renderer(code)))
    return patch_mini_host_renderer(code)


def patch_mini_host_renderer(code):
    code = replace_once(code, '      else if (k === "stop") player.stop();',
        '      else if (k === "stop") player.stop();\n      else if (k === "repeat") player.cycleRepeat();')
    code = replace_once(code, 'const np = t2 ? JSON.stringify([t2.title, t2.artist, this.playing, isLiked(t2), t2.thumbnail]) : "";',
        'const np = JSON.stringify([this.repeat, t2?.source, t2?.id, t2?.title, t2?.artist, this.playing, t2 && isLiked(t2), t2?.thumbnail]);')
    code = replace_once(code, '          durationSec: t2.durationSec,\n          id: t2.id\n        } : null);',
        '          durationSec: t2.durationSec,\n          id: t2.id,\n          trackKey: likeKeyOf(t2)\n        } : null, this.repeat);')
    code = replace_once(code, '''      if (Date.now() - this.lastProgressSent > 900) {
        this.lastProgressSent = Date.now();
        api.setProgress(this.time, this.duration);
      }''', '''      const trackKey = this.current ? likeKeyOf(this.current) : null;
      if (trackKey !== this.lastProgressTrackKey || Date.now() - this.lastProgressSent > 900) {
        this.lastProgressTrackKey = trackKey;
        this.lastProgressSent = Date.now();
        api.setProgress(this.time, this.duration, trackKey);
      }''')
    code = replace_once(code, '    seek(sec) {\n      if (!this.current) return;',
        '    seek(sec) {\n      if (!this.current) return;\n      this.lastProgressSent = 0;')
    return replace_once(code, '    api.onVolumeFromMini((v) => {', '''    api.onMiniSeek((fraction, trackKey) => {
      if (player.current && likeKeyOf(player.current) === trackKey && Number.isFinite(player.duration) && player.duration > 0) {
        player.seek(player.duration * fraction);
      }
    });
    api.onVolumeFromMini((v) => {''')


def patch_mini_main(code):
    code = replace_once(code, 'function miniState() {', 'var miniRepeatMode = "off";\nfunction miniState() {')
    code = replace_once(code, '    liked: nowPlaying2?.liked ?? false,\n    lang:',
        '    liked: nowPlaying2?.liked ?? false,\n    repeat: miniRepeatMode,\n    lang:')
    code = replace_once(code, '    playing: nowPlaying2?.playing ?? false,',
        '    playing: nowPlaying2?.playing ?? false,\n    buffering: nowPlaying2?.buffering ?? false,')
    code = replace_once(code, '      playing: !!info3.playing,',
        '      playing: !!info3.playing,\n      buffering: !!info3.buffering,')
    code = replace_once(code, '''    time: progress.time,
    duration: progress.duration,
    pinned:''', '''    trackKey: nowPlaying2?.trackKey,
    time: nowPlaying2 && progress.trackKey === nowPlaying2.trackKey ? progress.time : 0,
    duration: nowPlaying2 && progress.trackKey === nowPlaying2.trackKey ? progress.duration : 0,
    pinned:''')
    code = replace_once(code, '  const W = 400, H = 112;', '  const W = 424, H = 124;')
    code = replace_once(code, 'function createMini() {', '''function applyMiniPin(window, pinned) {
  if (!window || window.isDestroyed()) return;
  if (process.platform === "win32" && pinned) {
    window.setAlwaysOnTop(true, "screen-saver");
    if (window.isVisible() && !window.isMinimized()) window.moveTop();
  } else {
    window.setAlwaysOnTop(pinned);
  }
}
function createMini() {''')
    code = replace_once(code, '  mini.webContents.on("did-finish-load", pushMini);', '''  const window = mini;
  for (const event of ["show", "focus", "restore"]) {
    window.on(event, () => {
      if (getStore().mini?.pinned ?? true) applyMiniPin(window, true);
    });
  }
  if (process.platform === "win32") window.on("blur", () => {
    setTimeout(() => {
      if (!window.isDestroyed() && window.isVisible() && !window.isMinimized() && (getStore().mini?.pinned ?? true)) {
        applyMiniPin(window, true);
      }
    }, 100);
  });
  mini.webContents.on("did-finish-load", pushMini);''')
    code = replace_once(code, '''    win?.hide();
    mini.showInactive();
    pushMini();''', '''    win?.hide();
    if (mini.isMinimized()) mini.restore();
    mini.showInactive();
    pushMini();''')
    code = replace_once(code, '    mini?.setAlwaysOnTop(!!pinned);', '    applyMiniPin(mini, !!pinned);')
    code = replace_once(code, '''  if (link) void openLink(link);
  if (win) {''', '''  if (link) void openLink(link);
  else if (mini && !mini.isDestroyed() && (mini.isVisible() || mini.isMinimized())) {
    if (mini.isMinimized()) mini.restore();
    mini.show();
    mini.focus();
    return;
  }
  if (win) {''')
    code = replace_once(code, '''  import_electron15.ipcMain.on("setProgress", (e, time, duration) => {''',
        '''  import_electron15.ipcMain.on("miniSeek", (e, fraction, trackKey) => {
    if (e.senderFrame?.url !== MINI_URL || typeof fraction !== "number" || !Number.isFinite(fraction)) return;
    if (typeof trackKey !== "string" || nowPlaying2?.trackKey !== trackKey || progress.trackKey !== trackKey || !(progress.duration > 0)) return;
    send3("miniSeek", Math.max(0, Math.min(1, fraction)), trackKey);
  });
  import_electron15.ipcMain.on("setProgress", (e, time, duration, trackKey) => {''')
    code = replace_once(code, '    progress = { time: Number(time) || 0, duration: Number(duration) || 0 };',
        '    progress = { time: Number(time) || 0, duration: Number(duration) || 0, trackKey: typeof trackKey === "string" ? trackKey : null };')
    code = replace_once(code, '      liked: !!info3.liked,\n      thumbnail:',
        '      liked: !!info3.liked,\n      trackKey: typeof info3.trackKey === "string" ? info3.trackKey : null,\n      thumbnail:')
    code = replace_once(code, '''    if (fromMini(e) && (cmd === "playpause" || cmd === "next" || cmd === "prev")) mediaKey(cmd);''',
        '''    if (!fromMini(e)) return;
    if (cmd === "like") send3("trayLike");
    else if (cmd === "playpause" || cmd === "next" || cmd === "prev" || cmd === "repeat") mediaKey(cmd);''')
    return replace_once(code, '''  import_electron15.ipcMain.on("setNowPlaying", (e, info3) => {
    if (e.senderFrame?.url !== RENDERER_URL) return;''',
        '''  import_electron15.ipcMain.on("setNowPlaying", (e, info3, repeat) => {
    if (e.senderFrame?.url !== RENDERER_URL) return;
    miniRepeatMode = repeat === "one" || repeat === "all" ? repeat : "off";''')


def patch_mini_preload(code):
    code = replace_once(code, 'setNowPlaying: (info) => import_electron.ipcRenderer.send("setNowPlaying", info)',
        'setNowPlaying: (info, repeat) => import_electron.ipcRenderer.send("setNowPlaying", info, repeat)')
    return replace_once(code, 'setProgress: (time, duration) => import_electron.ipcRenderer.send("setProgress", time, duration),',
        '''setProgress: (time, duration, trackKey) => import_electron.ipcRenderer.send("setProgress", time, duration, trackKey),
  miniSeek: (fraction, trackKey) => import_electron.ipcRenderer.send("miniSeek", fraction, trackKey),
  onMiniSeek: (cb) => {
    import_electron.ipcRenderer.on("miniSeek", (_e, fraction, trackKey) => cb(fraction, trackKey));
  },''')


def patch_mini_html(code):
    code = replace_once(code, '        <span class="vol">', '''        <button id="like" title="Нравится" aria-pressed="false" disabled><i data-icon="heart"></i></button>
        <button id="repeat" title="Повтор выключен" aria-pressed="false"><i data-icon="repeat"></i></button>
        <span class="vol">''')
    return replace_once(code, '    <div class="bar"><div class="fill" id="fill"></div></div>',
        '    <input class="mini-seek" type="range" id="seek" min="0" max="1000" value="0" title="Перемотка" disabled />')


def patch_mini_css(code):
    code = replace_once(code, '.controls { display: flex; align-items: center; gap: 10px; }',
        '.controls { display: flex; align-items: center; gap: 6px; }\n.controls button { width: 28px; flex: none; }\n.controls button.play { width: 34px; }\n.controls button:disabled { opacity: .45; cursor: default; }\n.controls button:disabled:hover { color: var(--muted); background: none; }')
    code = replace_once(code, 'gap: 4px; margin-left: 6px; color: var(--muted);',
        'gap: 4px; margin-left: 0; color: var(--muted);')
    code = replace_once(code, 'width: 84px; height: 4px;', 'width: 52px; height: 4px;')
    code = replace_once(code, 'padding: 8px 8px 10px;', 'padding: 8px 8px 20px;')
    return replace_once(code, '''.bar { position: absolute; left: 0; right: 0; bottom: 0; height: 3px; background: #282828; }
.fill { height: 100%; width: 0; background: var(--accent); transition: width .4s linear; }''',
        '''.mini-seek { --progress: 0%; appearance: none; -webkit-appearance: none; -webkit-app-region: no-drag; position: absolute; left: 10px; right: 10px; bottom: 1px; width: calc(100% - 20px); height: 18px; margin: 0; padding: 0; background: transparent; cursor: pointer; }
.mini-seek::-webkit-slider-runnable-track { height: 4px; border-radius: 4px; background: linear-gradient(to right, var(--accent) var(--progress), #282828 var(--progress)); }
.mini-seek::-webkit-slider-thumb { appearance: none; -webkit-appearance: none; width: 10px; height: 10px; margin-top: -3px; border-radius: 50%; background: var(--accent); }
.mini-seek:disabled { opacity: .45; cursor: default; }
.mini-seek:disabled::-webkit-slider-thumb { visibility: hidden; }
.mini-seek:focus-visible { outline: 1px solid var(--accent); border-radius: 4px; }''')


def patch_mini_renderer(code, host_renderer):
    icons = "".join(
        line for line in host_renderer.splitlines(keepends=True)
        if line.startswith(("    heart:", "    heartFull:", "    repeat:", "    repeatOne:"))
    )
    if len(icons.splitlines()) != 4:
        raise ValueError("Expected the four shared like and repeat icons")
    code = replace_once(code, "  var ICONS = {\n", "  var ICONS = {\n" + icons)
    code = replace_once(code, '  $("play").onclick = () => api.miniCommand("playpause");',
        '''  $("play").onclick = () => api.miniCommand("playpause");
  $("like").onclick = () => api.miniCommand("like");
  $("repeat").onclick = () => api.miniCommand("repeat");''')
    code = replace_once(code, '    if (e.code === "Space") {',
        '    if (e.code === "Space" && !e.target.closest("button, input")) {')
    code = replace_once(code, '''    $("play").replaceChildren(icon(s.playing ? "pause" : "play"));
    $("pin").classList''', r'''    $("play").replaceChildren(icon(s.playing ? "pause" : "play"));
    const like = $("like");
    like.disabled = s.title == null;
    like.replaceChildren(icon(s.liked ? "heartFull" : "heart"));
    like.classList.toggle("on", !!s.liked);
    like.setAttribute("aria-pressed", String(!!s.liked));
    like.title = s.liked ? t("\u0423\u0431\u0440\u0430\u0442\u044C \u0438\u0437 \u043B\u044E\u0431\u0438\u043C\u043E\u0433\u043E") : t("\u0414\u043E\u0431\u0430\u0432\u0438\u0442\u044C \u0432 \u043B\u044E\u0431\u0438\u043C\u043E\u0435");
    const repeat = $("repeat");
    const repeating = s.repeat === "all" || s.repeat === "one";
    repeat.replaceChildren(icon(s.repeat === "one" ? "repeatOne" : "repeat"));
    repeat.classList.toggle("on", repeating);
    repeat.setAttribute("aria-pressed", String(repeating));
    repeat.title = s.repeat === "one" ? t("\u041F\u043E\u0432\u0442\u043E\u0440 \u043E\u0434\u043D\u043E\u0433\u043E \u0442\u0440\u0435\u043A\u0430") :
      s.repeat === "all" ? t("\u041F\u043E\u0432\u0442\u043E\u0440 \u043E\u0447\u0435\u0440\u0435\u0434\u0438 / \u043F\u043B\u0435\u0439\u043B\u0438\u0441\u0442\u0430") :
      t("\u041F\u043E\u0432\u0442\u043E\u0440 \u0432\u044B\u043A\u043B\u044E\u0447\u0435\u043D");
    $("pin").classList''')
    code = replace_once(code, '    if (!e.target.closest("button")) api.miniToggle();',
        '    if (!e.target.closest("button, input")) api.miniToggle();')
    code = replace_once(code, '    $("artist").textContent = s.artist ?? "";',
        '''    const artist = s.artist ?? "";
    const loading = s.lang === "en" ? "Buffering…" : "Буферизация…";
    $("artist").textContent = s.buffering ? loading + (artist ? " · " + artist : "") : artist;''')
    code = replace_once(code, '  api.onMiniState((s) => {\n    state = s;', '''  const seek = $("seek");
  let seekTrack = null;
  const clock = (seconds) => {
    const total = Math.floor(Math.max(0, seconds || 0));
    return Math.floor(total / 60) + ":" + String(total % 60).padStart(2, "0");
  };
  const paintSeek = () => {
    const fraction = Number(seek.value) / 1000;
    seek.style.setProperty("--progress", fraction * 100 + "%");
    seek.title = state.lang === "en" ? "Seek" : "Перемотка";
    seek.setAttribute("aria-valuetext", clock(fraction * state.duration) + " / " + clock(state.duration));
  };
  const restoreSeek = () => {
    seek.value = String(!seek.disabled ? Math.round(Math.max(0, Math.min(1, state.time / state.duration)) * 1000) : 0);
    paintSeek();
  };
  seek.addEventListener("input", () => {
    if (seekTrack === null) seekTrack = state.trackKey;
    paintSeek();
  });
  seek.addEventListener("change", () => {
    if (!seek.disabled && seekTrack === state.trackKey) api.miniSeek(Number(seek.value) / 1000, seekTrack);
    else restoreSeek();
    seekTrack = null;
  });
  seek.addEventListener("pointercancel", () => {
    seekTrack = null;
    restoreSeek();
  });
  api.onMiniState((s) => {
    state = s;
    seek.disabled = !s.trackKey || !(s.duration > 0);
    if (seekTrack !== s.trackKey || seek.disabled) {
      // The old drag key is kept until release so a track change cannot seek the new track.
      restoreSeek();
    } else {
      paintSeek();
    }''')
    line, = (line for line in code.splitlines(keepends=True) if '$("fill").style.width' in line)
    return replace_once(code, line, "")


def patch_discord_art(code):
    return replace_once(code, '''      ...cover ? { large_image: cover, large_text: clip(t2.album || t2.title) } : {},
      small_image: "kaban",''',
        '''      ...cover ? { large_image: cover, large_text: clip(t2.album || t2.title) } :
        clientId === BUILTIN_CLIENT_ID ? { large_image: "https://mrsetefed.github.io/kabanmusic/assets/kaban_no_cover.gif", large_text: "KabanMusic" } : {},
      small_image: "kaban",''')


def patch_playback_stutter_main(code):
    code = replace_once(code, '''    await exec(HOLD_JS(videoId));
  }
  if (revision === playbackRevision) loadedAt = Date.now();''',
        '''    await exec(HOLD_JS(videoId, true));
  }
  if (revision === playbackRevision) loadedAt = Date.now();''')
    code = replace_once(code, '''  await exec(HOLD_JS(videoId));
  for (let i2 = 0; i2 < 20; i2++) {''',
        '''  await exec(HOLD_JS(videoId, true));
  for (let i2 = 0; i2 < 20; i2++) {''')
    code = replace_once(code, "var HOLD_JS = (videoId) =>", "var HOLD_JS = (videoId, pinOnNavigation = false) =>")
    code = replace_once(code, "  clearInterval(window.__ptFade);const v=document.querySelector('video');",
        """  if(${pinOnNavigation}){
    try{document.getElementById('movie_player')?.setPlaybackQualityRange?.('tiny','tiny');}catch(error){}
  }
  clearInterval(window.__ptFade);const v=document.querySelector('video');""")
    start = code.index("      // we only need the sound: keep the video at the lowest quality")
    end = code.index("      const vd=", start)
    code = code[:start] + code[end:]
    code = replace_once(code, """      const dg=window.__ptDiagEv?window.__ptDiagEv.splice(0):[];
      return {id:vd?vd.video_id:null, st:p.getPlayerState(),""",
        '''      const media=document.querySelector('video');
      const playerState=p.getPlayerState();
      const waiting=!!media&&!media.paused&&!media.ended&&media.readyState<3;
      ${QUALITY_GUARD_JS};
      const dg=window.__ptDiagEv?window.__ptDiagEv.splice(0):[];
      return {id:vd?vd.video_id:null, st:playerState, waiting,''')
    code = replace_once(code, "      state,\n      time: s.t || 0,", "      state,\n      waiting: s.waiting,\n      time: s.t || 0,")
    code = replace_once(code, "    if (s.dg?.length) logHiccups(s.id, s.dg);",
        '''    const diagnostics = s.dg?.filter((event) => event.id === s.id).map((event) => event.line);
    if (diagnostics?.length) logHiccups(s.id, diagnostics);''')
    start = code.index("var DIAG_JS = ")
    end = code.index("var logBusy", start)
    marker = chr(96)
    quality_guard = r'''(()=>{
  const id=vd?.video_id, loadAt=window.__ptLoadAt;
  if(!id||id!==window.__ptHoldId||!Number.isFinite(loadAt)) return;
  const now=performance.now();
  let drift=window.__ptQualityDrift;
  if(!drift||drift.id!==id||drift.loadAt!==loadAt){
    drift=window.__ptQualityDrift={id,loadAt,quality:null,since:0,lastPin:-Infinity};
  }
  if(ad||window.__ptHold||playerState!==1||!media||media.paused||media.seeking||waiting){
    drift.quality=null;return;
  }
  const quality=p.getPlaybackQuality?.();
  if(!quality||quality==='tiny'||quality==='auto'||quality==='unknown'){
    drift.quality=null;return;
  }
  if(quality!==drift.quality){
    drift.quality=quality;drift.since=now;return;
  }
  // The initial load already pins tiny. Only force another switch after persistent drift.
  if(now-loadAt<3500||now-drift.since<750||now-drift.lastPin<12000||
    !p.setPlaybackQualityRange) return;
  drift.lastPin=now;
  let result='requested';
  try{
    p.setPlaybackQualityRange('tiny','tiny');
    p.setPlaybackQuality?.('tiny');
  }catch(error){result='failed';}
  if(window.__ptDiagEv?.length<50){
    const elapsed=((now-loadAt)/1000).toFixed(2);
    window.__ptDiagEv.push({id,line:'quality pin '+result+' '+quality+' -> tiny at t='+
      media.currentTime.toFixed(2)+'s ('+elapsed+'s after load)'});
  }
})()'''
    diagnostics = r'''(()=>{if(window.__ptDiag)return;window.__ptDiag=true;window.__ptDiagEv=[];
  let previous=null, freeze=null;
  setInterval(()=>{
    const v=document.querySelector('video'),p=document.getElementById('movie_player');
    const id=p?.getVideoData?.()?.video_id, state=p?.getPlayerState?.();
    if(!v||!id||id!==window.__ptHoldId||window.__ptHold||v.paused||v.ended||v.seeking||
      (state!==1&&state!==3)||p.classList.contains('ad-showing')||
      v.currentTime<0.3||(Number.isFinite(v.duration)&&v.duration-v.currentTime<0.25)){
      previous=null;freeze=null;return;
    }
    const now=performance.now(), time=v.currentTime, loadAt=window.__ptLoadAt;
    const same=previous&&previous.id===id&&previous.v===v&&previous.loadAt===loadAt;
    const delta=same?time-previous.time:Infinity;
    if(Math.abs(delta)<0.001){
      if(!freeze){
        let ahead=0, stats='';
        for(let i=0;i<v.buffered.length;i++){
          if(v.buffered.start(i)<=time&&time<v.buffered.end(i)) ahead=v.buffered.end(i)-time;
        }
        try{const info=p.getStatsForNerds();stats=(info.codecs||'')+' '+(info.resolution||'');}catch(e){}
        freeze={since:previous.now,time,after:Math.max(0,previous.now-loadAt),ahead,ready:v.readyState,
          quality:p.getPlaybackQuality?.()||'unknown',stats};
      }
    }else{
      if(same&&delta>0&&delta<0.75&&freeze&&now-freeze.since>=120&&window.__ptDiagEv.length<50){
        const f=freeze;
        const line='freeze '+Math.round(now-f.since)+'ms at t='+f.time.toFixed(2)+'s ('+
          (f.after/1000).toFixed(2)+'s after load) buffer+'+f.ahead.toFixed(1)+'s ready='+f.ready+' q='+f.quality+' '+f.stats;
        window.__ptDiagEv.push({id,line});
      }
      freeze=null;
    }
    previous={id,v,loadAt,now,time};
  },20);})()'''
    scripts = (
        "var QUALITY_GUARD_JS = " + marker + quality_guard + marker + ";\n"
        "var DIAG_JS = " + marker + diagnostics + marker + ";\n"
    )
    return code[:start] + scripts + code[end:]


def patch_youtube_single_pin(code):
    # Keep the tiny range before loading, without reapplying it to the newly loading player.
    code = replace_once(code, """      p.loadVideoById(${JSON.stringify(videoId)});
      p.setPlaybackQualityRange&&p.setPlaybackQualityRange('tiny','tiny');return true})()`""",
        """      p.loadVideoById(${JSON.stringify(videoId)});return true})()`""")
    code = replace_once(code, "  let previous=null, freeze=null;", "  let previous=null, freeze=null, qualitySample=null;")
    return replace_once(code, """    const now=performance.now(), time=v.currentTime, loadAt=window.__ptLoadAt;
    const same=previous""",
        """    const now=performance.now(), time=v.currentTime, loadAt=window.__ptLoadAt;
    if(!qualitySample||qualitySample.id!==id||qualitySample.loadAt!==loadAt||now-qualitySample.at>=100){
      const sameLoad=qualitySample?.id===id&&qualitySample.loadAt===loadAt;
      const last=sameLoad?qualitySample.quality:null;
      const quality=p.getPlaybackQuality?.();
      if(quality&&quality!=='unknown'&&quality!==last&&window.__ptDiagEv.length<50){
        window.__ptDiagEv.push({id,line:'quality '+(last?last+' -> ':'')+quality+' at t='+
          time.toFixed(2)+'s ('+((now-loadAt)/1000).toFixed(2)+'s after load)'});
      }
      qualitySample={id,loadAt,at:now,quality:quality&&quality!=='unknown'?quality:last};
    }
    const same=previous""")


def patch_playback_stutter_renderer(code):
    line, = (line for line in code.splitlines(keepends=True) if 'this.audio.addEventListener("waiting",' in line)
    code = replace_once(code, line, line.replace('this.engine === "audio" &&', 'this.engine === "audio" && !this.audio.paused &&'))
    code = replace_once(code, '''    pause() {
      void api.ytCommand("pause");
      this.audio.pause();
      this.playing = false;''', '''    pause() {
      void api.ytCommand("pause");
      this.audio.pause();
      this.playing = false;
      this.buffering = false;''')
    code = replace_once(code, '      this.buffering = s.state === 3 || s.state === -1;',
        '      this.buffering = s.state !== 2 && (s.waiting === true || s.state === 3 || s.state === -1);')
    code = replace_once(code, '''        if (seq !== this.loadSeq) return;
        this.playing = true;
        void api.nowPlaying(t2);''',
        '''        if (seq !== this.loadSeq) return;
        if (this.engine === "audio") this.playing = !this.audio.paused;
        void api.nowPlaying(t2);''')
    code = replace_once(code, '''      } catch (e) {
        if (seq !== this.loadSeq) return;
        toast(errMsg(e), true);
      }
      this.render();
      markPlaying();''', '''      } catch (e) {
        if (seq !== this.loadSeq) return;
        this.buffering = false;
        toast(errMsg(e), true);
      }
      this.render();
      markPlaying();''')
    code = replace_once(code, 'this.playing, t2 && isLiked(t2), t2?.thumbnail]',
        'this.playing, this.buffering, t2 && isLiked(t2), t2?.thumbnail]')
    return replace_once(code, '          playing: this.playing,\n          liked: isLiked(t2),',
        '          playing: this.playing,\n          buffering: this.buffering,\n          liked: isLiked(t2),')


def patch_startup_main(code):
    line, = (line for line in code.splitlines(keepends=True) if line.startswith("var ACRYLIC = "))
    code = replace_once(code, line, "")
    code = replace_once(code, '''    // Windows 11 (22H2+): acrylic behind the black frame — the CSS paints it at 82 % opacity for a slight see-through.
    ...ACRYLIC ? { backgroundMaterial: "acrylic", backgroundColor: "#00000000" } : { backgroundColor: "#000000" },
    ...process.platform === "win32" ? { accentColor: "#161616" } : {},''',
        '''    backgroundColor: "#000000",
    ...process.platform === "win32" ? { backgroundMaterial: "none", accentColor: "#161616" } : {},''')
    code = replace_once(code, 'titleBarOverlay: { color: ACRYLIC ? "#00000000" : "#000000",',
        'titleBarOverlay: { color: "#000000",')
    code = replace_once(code, '''  createEngine();
  setInterval(poll, 500);''',
        '''  setInterval(poll, 500);''')
    code = replace_once(code, 'function startEngine() {\n  startEngineNow();',
        'function startEngine() {\n  if (!engine || engine.isDestroyed()) createEngine();\n  startEngineNow();')
    code = replace_once(code, 'async function login() {\n  if (!engine) return;',
        '''async function login() {
  if (!engine || engine.isDestroyed()) {
    createEngine();
    startEngineNow = () => {};
    engineReady = Promise.resolve();
  }''')
    code = replace_once(code, 'async function load(videoId) {\n',
        'async function load(videoId) {\n  if (isLoginWindow) throw new Error(t("Заверши вход в YouTube Music и попробуй ещё раз."));\n')
    code = replace_once(code, 'var EN = {', '''var EN = {
  "Заверши вход в YouTube Music и попробуй ещё раз.": "Finish signing in to YouTube Music and try again.",''')
    code = replace_once(code, '''  win.webContents.once("did-finish-load", () => {
    mark("ui");
    uiReady = true;
    if (pendingLink) setTimeout(() => (send3("openLink", pendingLink), pendingLink = null), 500);
    setTimeout(writeStartupLog, 50);
  });
  win.once("ready-to-show", () => {
    mark("shown");
    if (saved?.maximized) win?.maximize();
    if (!process.argv.includes("--hidden")) win?.show();
    setTimeout(() => {
      startEngine();
      void refreshAccountInfo().then((changed2) => changed2 && send3("accountChange"));
    }, 600);
  });
  void win.loadFile(RENDERER);''',
        '''  win.webContents.once("did-finish-load", () => mark("document"));
  win.once("ready-to-show", () => mark("paint"));
  void win.loadFile(RENDERER);
  if (!process.argv.includes("--hidden")) {
    if (saved?.maximized) win.maximize();
    win.show();
    mark("shown");
  } else if (saved?.maximized) {
    win.once("show", () => win?.maximize());
  }''')
    code = replace_once(code, '  handle("appVersion", () => import_electron15.app.getVersion());',
        '''  handle("appVersion", () => import_electron15.app.getVersion());
  handle("startupPrefs", () => {
    const { lang, volume, playMode } = getStore();
    return { lang, volume, playMode, version: import_electron15.app.getVersion() };
  });''')
    code = replace_once(code, '  import_electron15.ipcMain.on("miniToggle",',
        '''  import_electron15.ipcMain.on("uiLoaded", (e) => {
    if (!fromUi(e) || uiReady) return;
    uiReady = true;
    mark("interactive");
    if (pendingLink) {
      const link = pendingLink;
      pendingLink = null;
      send3("openLink", link);
    }
    setTimeout(writeStartupLog, 150);
    setTimeout(() => {
      if (quitting2) return;
      if (!isLoginWindow) startEngine();
      void refreshAccountInfo().then((changed2) => changed2 && send3("accountChange"));
    }, 1600);
  });
  import_electron15.ipcMain.on("miniToggle",''')
    code = replace_once(code, '''  const firstLink = process.argv.find((a) => a.startsWith("kabanmusic://"));
  if (firstLink) void openLink(firstLink);
''', "")
    code = replace_once(code, '''  init(() => send3("localChange"));
  ensureDownloads(() => all().filter((t2) => t2.downloaded).sort((a, b) => b.mtime - a.mtime));
  initYt({''', "  initYt({")
    code = replace_once(code, '''  createWindow();
  setupShell();''', '''  createWindow();
  init(() => send3("localChange"));
  ensureDownloads(() => all().filter((t2) => t2.downloaded).sort((a, b) => b.mtime - a.mtime));
  const firstLink = process.argv.find((a) => a.startsWith("kabanmusic://"));
  if (firstLink) void openLink(firstLink);
  setupShell();''')
    code = replace_once(code, '  const loggedIn2 = await isLoggedIn();\n  return {\n    google:',
        '  const [loggedIn2, vkLoggedIn] = await Promise.all([isLoggedIn(), loggedIn()]);\n  return {\n    google:')
    return replace_once(code, '    vk: { loggedIn: await loggedIn() }',
        '    vk: { loggedIn: vkLoggedIn }')


def patch_startup_renderer(code):
    code = replace_once(code, '''    const st0 = await api.status().catch(() => null);
    APP_VERSION = await api.appVersion().catch(() => "");''',
        '''    const prefs = await api.startupPrefs().catch(() => null);
    APP_VERSION = prefs?.version ?? "";''')
    code = replace_once(code, '    if (st0?.lang === "en") {', '    if (prefs?.lang === "en") {')
    code = replace_once(code, '''    void api.status().then((st) => {
      player.volume = st.volume;
      syncVolumeUi();
      player.setPlayMode(st.playMode);
    });
    player.render();''', '''    if (prefs) {
      player.volume = prefs.volume;
      player.setPlayMode(prefs.playMode);
      syncVolumeUi();
    }
    player.render();''')
    code = replace_once(code, '    window.__uiReadyAt = Math.round(performance.now());',
        '    window.__uiReadyAt = Math.round(performance.now());\n    api.uiLoaded();')
    start = code.index("  async function viewHome()")
    end = code.index("  var lastQuery", start)
    home = code[start:end]
    prefix = '      h("div", { class: "sub" }, status2?.google.loggedIn ?'
    line, = (line for line in home.splitlines(keepends=True) if line.startswith(prefix))
    message = line.removeprefix('      h("div", { class: "sub" }, ').removesuffix("),\n")
    home = replace_once(home, "    const status2 = await api.status().catch(() => null);",
        '    const subtitle = h("div", { class: "sub", hidden: true });')
    home = replace_once(home, line, "      subtitle,\n")
    home = replace_once(home, "    void api.historyList(0, 200).then((entries) => {",
        f'''    void api.status().then((status2) => {{
      if (!subtitle.isConnected) return;
      subtitle.textContent = {message};
      subtitle.hidden = false;
    }}).catch(() => {{}});
    void api.historyList(0, 200).then((entries) => {{''')
    return code[:start] + home + code[end:]


def patch_startup_preload(code):
    return replace_once(code, '  appVersion: inv("appVersion"),', '''  appVersion: inv("appVersion"),
  startupPrefs: inv("startupPrefs"),
  uiLoaded: () => import_electron.ipcRenderer.send("uiLoaded"),''')


def patch_window_shell_main(code):
    code = replace_once(code, 'var import_node_os3 = require("node:os");\n', "")
    code = replace_once(code, '''    backgroundColor: "#000000",
    ...process.platform === "win32" ? { backgroundMaterial: "none", accentColor: "#161616" } : {},
    autoHideMenuBar: true,
    // own dark title bar; Windows still draws min/max/close (and snap layouts) on top of it
    titleBarStyle: "hidden",
    titleBarOverlay: { color: "#000000", symbolColor: "#b3b3b3", height: 32 },''',
        '''    backgroundColor: "#000000",
    frame: false,
    ...process.platform === "win32" ? { backgroundMaterial: "none", accentColor: "#000000", roundedCorners: false } : {},
    autoHideMenuBar: true,''')
    code = replace_once(code, '''  win.webContents.once("did-finish-load", () => mark("document"));
  win.once("ready-to-show", () => mark("paint"));
  void win.loadFile(RENDERER);
  if (!process.argv.includes("--hidden")) {
    if (saved?.maximized) win.maximize();
    win.show();
    mark("shown");
  } else if (saved?.maximized) {
    win.once("show", () => win?.maximize());
  }
  updateThumbar();''',
        '''  mark("window-created");
  const window = win;
  const publishBounds = () => send3("windowMaximized", window.isMaximized());
  window.on("maximize", publishBounds);
  window.on("unmaximize", publishBounds);
  window.webContents.once("did-finish-load", () => mark("document"));
  window.once("ready-to-show", () => {
    mark("paint");
    if (process.argv.includes("--hidden") || window.isVisible()) return;
    window.show();
    mark("shown");
    if (uiReady) setTimeout(writeStartupLog, 150);
  });
  if (saved?.maximized) window.once("show", () => window.maximize());
  void window.loadFile(RENDERER);''')
    code = replace_once(code, '    mark("interactive");\n    if (pendingLink) {',
        '    mark("interactive");\n    send3("windowMaximized", win?.isMaximized() ?? false);\n    if (pendingLink) {')
    code = replace_once(code, '''    setTimeout(writeStartupLog, 150);
    setTimeout(() => {''', '''    setTimeout(writeStartupLog, win?.isVisible() || process.argv.includes("--hidden") ? 150 : 4000);
    setTimeout(() => {
      if (!quitting2) finishWindowsStartup();
    }, 600);
    setTimeout(() => {''')
    code = replace_once(code, '  import_electron15.ipcMain.on("uiLoaded",',
        '''  import_electron15.ipcMain.on("windowAction", (e, action) => {
    if (!fromUi(e) || !win || win.isDestroyed()) return;
    if (action === "minimize") win.minimize();
    else if (action === "toggleMaximize") win.isMaximized() ? win.unmaximize() : win.maximize();
    else if (action === "close") win.close();
  });
  import_electron15.ipcMain.on("uiLoaded",''')
    code = replace_once(code, '''  if (!import_electron15.app.isPackaged) import_electron15.app.setAsDefaultProtocolClient("kabanmusic", process.execPath, [(0, import_node_path16.join)(__dirname, "..")]);
  else import_electron15.app.setAsDefaultProtocolClient("kabanmusic");
''', "")
    code = replace_once(code, '''  import_electron15.protocol.handle("kaban-vk", (req) => serve2(req));
  initYt({''',
        '''  import_electron15.protocol.handle("kaban-vk", (req) => serve2(req));
  mark("ipc");
  createWindow();
  initYt({''')
    code = replace_once(code, '''  createWindow();
  init(() => send3("localChange"));''',
        '''  mark("youtube-session");
  init(() => send3("localChange"));''')
    code = replace_once(code, 'function writeStartupLog() {\n  try {',
        '''var startupLogWritten = false;
function writeStartupLog() {
  if (startupLogWritten) return;
  startupLogWritten = true;
  try {''')
    return replace_once(code, '''  setupShell();
  ensureStartMenuShortcut();
  applyAutostart();
});
var wasLoggedIn''',
        '''});
function finishWindowsStartup() {
  setupShell();
  updateThumbar();
  ensureStartMenuShortcut();
  applyAutostart();
  if (!import_electron15.app.isPackaged) import_electron15.app.setAsDefaultProtocolClient("kabanmusic", process.execPath, [(0, import_node_path16.join)(__dirname, "..")]);
  else import_electron15.app.setAsDefaultProtocolClient("kabanmusic");
}
var wasLoggedIn''')


def patch_native_window_startup(code):
    start = code.index("function createWindow() {")
    end = code.index("function send3(", start)
    window = code[start:end]
    window = replace_once(window, '  const saved = getStore().window;\n  win = new',
        '  const saved = getStore().window;\n  mark("before-window");\n  win = new')
    window = replace_once(window, '    icon: asset2("icon.png"),\n', "")
    window = replace_once(window,
        '    ...process.platform === "win32" ? { backgroundMaterial: "none", accentColor: "#000000", roundedCorners: false } : {},',
        '    ...process.platform === "win32" ? { thickFrame: false } : {},')
    window = replace_once(window, '  import_electron15.Menu.setApplicationMenu(null);',
        '  mark("native-window");')
    window = replace_once(window, '  mark("window-created");',
        '  mark("window-hooks");')
    code = code[:start] + window + code[end:]
    return replace_once(code, '''function finishWindowsStartup() {
  setupShell();''', '''function finishWindowsStartup() {
  import_electron15.Menu.setApplicationMenu(null);
  if (process.platform !== "darwin") win?.setIcon(asset2("icon.ico"));
  setupShell();''')


def patch_startup_diagnostics(code):
    code = replace_once(code, '  mark("before-window");\n  win = new',
        '''  import_electron15.app.once("web-contents-created", () => mark("web-contents"));
  import_electron15.app.once("browser-window-created", () => mark("browser-window"));
  const windowCpuStart = process.cpuUsage();
  mark("before-window");
  win = new''')
    code = replace_once(code, '  mark("native-window");',
        '''  mark("native-window");
  const windowCpu = process.cpuUsage(windowCpuStart);
  marks.push(`window-cpu ${Math.round((windowCpu.user + windowCpu.system) / 1e3)}`);''')
    return replace_once(code, '    const old = (0, import_node_fs14.existsSync)(f)',
        '''    const browserProcess = import_electron15.app.getAppMetrics().find((metric) => metric.pid === process.pid);
    if (browserProcess?.creationTime > 0) {
      marks.unshift(`before-js ${Math.max(0, Math.round(T0 - browserProcess.creationTime))}`);
    }
    const old = (0, import_node_fs14.existsSync)(f)''')


def patch_update_copy(code):
    code = replace_once(code, 'var import_node_path = require("node:path");',
        '''var import_node_path = require("node:path");
// The updater copies app.asar itself, not Electron's virtual files inside it.
var updateFs = require("original-fs");''')
    code = replace_once(code, '''        (0, import_node_fs.cpSync)(src, appDir, { recursive: true, force: true });
        log(`copied (attempt ${attempt})`);''',
        '''        let unchanged = 0;
        const fromBuffer = Buffer.alloc(1024 * 1024);
        const toBuffer = Buffer.alloc(fromBuffer.length);
        const fromArchive = import_node_path.join(src, "resources", "app.asar");
        const toArchive = import_node_path.join(appDir, "resources", "app.asar");
        const hasArchive = updateFs.lstatSync(fromArchive, { throwIfNoEntry: false })?.isFile();
        updateFs.cpSync(src, appDir, {
          recursive: true,
          force: true,
          filter(from, to) {
            if (hasArchive && from === fromArchive) return false;
            if (!sameUpdateFile(from, to, fromBuffer, toBuffer)) return true;
            unchanged++;
            return false;
          }
        });
        if (hasArchive) replaceUpdateArchive(fromArchive, toArchive);
        log(`copied (attempt ${attempt}, ${unchanged} files unchanged)`);''')
    return replace_once(code, 'function alive(pid) {', '''function replaceUpdateArchive(from, to) {
  const stagingDir = updateFs.mkdtempSync(import_node_path.join(import_node_path.dirname(to), ".kaban-update-"));
  const stagedArchive = import_node_path.join(stagingDir, "app.asar");
  try {
    updateFs.copyFileSync(from, stagedArchive);
    updateFs.renameSync(stagedArchive, to);
  } finally {
    updateFs.rmSync(stagedArchive, { force: true });
    updateFs.rmdirSync(stagingDir);
  }
}
// Preserve known-good runtime files when an update only changes app.asar.
function sameUpdateFile(from, to, fromBuffer, toBuffer) {
  const source = updateFs.lstatSync(from);
  if (!source.isFile()) return false;
  const target = updateFs.lstatSync(to, { throwIfNoEntry: false });
  if (!target?.isFile() || target.size !== source.size) return false;
  const fromFd = updateFs.openSync(from, "r");
  try {
    const toFd = updateFs.openSync(to, "r");
    try {
      for (let offset = 0; offset < source.size; offset += fromBuffer.length) {
        const length = Math.min(fromBuffer.length, source.size - offset);
        if (updateFs.readSync(fromFd, fromBuffer, 0, length, offset) !== length ||
            updateFs.readSync(toFd, toBuffer, 0, length, offset) !== length ||
            !fromBuffer.subarray(0, length).equals(toBuffer.subarray(0, length))) return false;
      }
      return true;
    } finally {
      updateFs.closeSync(toFd);
    }
  } finally {
    updateFs.closeSync(fromFd);
  }
}
function alive(pid) {''')


def patch_window_shell_html(code):
    return replace_once(code, '  <div class="titlebar"><img src="../assets/icon.png" alt="" /><span id="titleText">KabanMusic</span></div>',
        '''  <div class="titlebar">
    <img src="../assets/icon.png" alt="" /><span id="titleText">KabanMusic</span>
    <div class="window-controls">
      <button id="windowMinimize" title="Свернуть"><span aria-hidden="true"></span></button>
      <button id="windowMaximize" title="Развернуть"><span aria-hidden="true"></span></button>
      <button id="windowClose" title="Закрыть"><span aria-hidden="true"></span></button>
    </div>
  </div>''')


def patch_window_shell_css(code):
    code = replace_once(code, 'user-select: none; padding-right: 150px; }',
        'user-select: none; padding-right: 0; }')
    return replace_once(code, '.titlebar span { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }',
        '''.titlebar #titleText { flex: 1; min-width: 0; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.window-controls { display: flex; align-self: stretch; flex: none; -webkit-app-region: no-drag; }
.window-controls button { position: relative; width: 46px; display: grid; place-items: center; color: #b3b3b3; }
.window-controls button:hover { color: #fff; background: #262626; }
.window-controls button:focus-visible { outline: 1px solid var(--accent); outline-offset: -2px; }
.window-controls #windowClose:hover { background: #c42b1c; }
.window-controls span { width: 10px; height: 10px; display: block; }
#windowMinimize span { height: 0; border-top: 1px solid currentColor; }
#windowMaximize span { border: 1px solid currentColor; }
#windowMaximize.is-maximized span { width: 8px; height: 8px; transform: translate(-1px, 1px); box-shadow: 2px -2px #000, 3px -3px currentColor; }
#windowClose span::before, #windowClose span::after { content: ""; position: absolute; width: 12px; height: 1px; left: calc(50% - 6px); top: 50%; background: currentColor; transform: rotate(45deg); }
#windowClose span::after { transform: rotate(-45deg); }''')


def patch_typography_css(code):
    code = replace_once(code, "  --muted: #b3b3b3;", """  --muted: #b3b3b3;
  --artist-text: #c7c7c7;
  --track-meta: #969696;""")
    code = replace_once(code, "h1 { font-size: 32px; margin:",
        "h1 { font-size: 34px; line-height: 1.15; margin:")
    code = replace_once(code, "h2 { font-size: 20px; margin:",
        "h2 { font-size: 22px; line-height: 1.25; font-weight: 650; margin:")
    code = replace_once(code, """.t-title { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; font-weight: 600; }
.t-artist, .t-album, .t-reason { color: var(--muted); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; font-size: 13px; }
.t-reason { color: var(--dim); font-size: 12px; }
.t-dur { color: var(--muted); text-align: right; font-variant-numeric: tabular-nums; }""",
        """.t-title { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; font-size: 15px; line-height: 20px; font-weight: 650; }
.t-artist, .t-album, .t-reason { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.t-artist { color: var(--artist-text); font-size: 13px; line-height: 18px; }
.t-album, .t-reason, .t-dur, .t-num { color: var(--track-meta); font-size: 12px; }
.t-dur { text-align: right; font-variant-numeric: tabular-nums; }""")
    code = replace_once(code, ".card .c-title { font-weight: 600;",
        ".card .c-title { font-size: 15px; font-weight: 650;")
    code = replace_once(code, ".np-title { font-weight: 600;",
        ".np-title { font-size: 16px; line-height: 22px; font-weight: 650;")
    return replace_once(code,
        ".np-artist { color: var(--muted); font-size: 13px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; display: flex; gap: 6px; align-items: center; }",
        """.np-artist { color: var(--artist-text); font-size: 13px; display: flex; gap: 6px; align-items: center; }
.np-artist-name { min-width: 0; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.np-artist .badge { flex: none; }""")


def patch_window_shell_renderer(code):
    code = replace_once(code, '[badge(t2.source), t2.artist] : []',
        '[h("span", { class: "np-artist-name", title: t2.artist }, t2.artist), badge(t2.source)] : []')
    code = replace_once(code, '    "Переношу из YouTube Music…": "Importing from YouTube Music…",',
        '''    "Переношу из YouTube Music…": "Importing from YouTube Music…",
    "Свернуть": "Minimize",
    "Развернуть": "Maximize",
    "Восстановить окно": "Restore window",''')
    code = replace_once(code, '.sidebar button, .sidebar-playlist-group summary, .np button, #menu button',
        '.sidebar button, .sidebar-playlist-group summary, .np button, #menu button, .window-controls button')
    return replace_once(code, '    hydrateIcons();\n    document.querySelectorAll(".nav")',
        '''    hydrateIcons();
    $("windowMinimize").onclick = () => api.windowAction("minimize");
    $("windowMaximize").onclick = () => api.windowAction("toggleMaximize");
    $("windowClose").onclick = () => api.windowAction("close");
    api.onWindowMaximized((maximized) => {
      const button = $("windowMaximize");
      button.classList.toggle("is-maximized", maximized);
      button.title = t(maximized ? "Восстановить окно" : "Развернуть");
    });
    document.querySelectorAll(".nav")''')


def patch_window_shell_preload(code):
    return replace_once(code, '  uiLoaded: () => import_electron.ipcRenderer.send("uiLoaded"),',
        '''  uiLoaded: () => import_electron.ipcRenderer.send("uiLoaded"),
  windowAction: (action) => import_electron.ipcRenderer.send("windowAction", action),
  onWindowMaximized: (cb) => import_electron.ipcRenderer.on("windowMaximized", (_event, value) => cb(value)),''')


def split_hls(code):
    start = code.index("  // node_modules/.pnpm/hls.js@")
    end = code.index("  // src/renderer/app.ts", start)
    library = '"use strict";\n(() => {\n' + code[start:end] + "  window.KabanHls = Hls;\n})();\n"
    return code[:start] + code[end:], library


def patch_lazy_hls(code):
    code = replace_once(code, '  var api = window.kaban;',
        '''  var api = window.kaban;
  let hlsModule;
  function loadHls() {
    hlsModule ??= new Promise((resolve, reject) => {
      const script = document.createElement("script");
      script.src = "./hls.js";
      script.onload = () => {
        if (typeof window.KabanHls === "function") resolve(window.KabanHls);
        else reject(new Error("HLS player could not be initialized"));
      };
      script.onerror = () => reject(new Error("HLS player could not be loaded"));
      document.head.append(script);
    }).catch((error) => {
      hlsModule = null;
      throw error;
    });
    return hlsModule;
  }''')
    return replace_once(code, '    playHls(url) {\n      this.hls?.destroy();',
        '''    async playHls(url) {
      const revision = this.loadSeq;
      const Hls = await loadHls();
      if (revision !== this.loadSeq) return;
      this.hls?.destroy();''')


def patch_asar(asar, version=None, ui_fix=False, youtube_single_pin=False):
    base = 8 + struct.unpack_from("<I", asar, 4)[0]
    length = struct.unpack_from("<I", asar, 12)[0]
    header = json.loads(asar[16:16 + length])
    body = bytearray()
    patches = {"dist/main.js": patch_main, "dist/renderer/app.js": patch_renderer}
    hls_library = None
    if ui_fix:
        def read_packed(path):
            item = dict(entries(header["files"]))[path]
            start = base + int(item["offset"])
            return asar[start:start + item["size"]].decode()

        host_renderer = read_packed("dist/renderer/app.js")
        _, hls_library = split_hls(host_renderer)
        patches["dist/boot.js"] = patch_update_copy
        patches["dist/main.js"] = lambda code: patch_startup_diagnostics(patch_native_window_startup(patch_window_shell_main(patch_startup_main(
            patch_playback_stutter_main(patch_discord_art(patch_mini_main(patch_playlist_merge_main(
                patch_windows_integration(patch_youtube_import_main(patch_main(code)))))))))))
        patches["dist/preload.js"] = lambda code: patch_window_shell_preload(patch_startup_preload(patch_mini_preload(replace_once(code, '  playlist: inv("playlist"),',
            '  playlist: inv("playlist"),\n  importYoutubePlaylist: inv("importYoutubePlaylist"),\n  mergeExternalPlaylist: inv("mergeExternalPlaylist"),'))))
        patches["dist/renderer/app.js"] = lambda code: patch_window_shell_renderer(patch_lazy_hls(patch_startup_renderer(
            patch_playback_stutter_renderer(patch_ui_renderer(patch_renderer(split_hls(code)[0]))))))
        patches["dist/renderer/index.html"] = lambda code: patch_window_shell_html(patch_ui_html(code))
        patches["dist/renderer/styles.css"] = lambda code: patch_typography_css(patch_window_shell_css(patch_ui_css(
            replace_once(code, "--chrome: rgba(0, 0, 0, 0.82);", "--chrome: #000;"))))
        patches["dist/renderer/mini.html"] = patch_mini_html
        patches["dist/renderer/mini.css"] = patch_mini_css
        patches["dist/renderer/mini.js"] = lambda code: patch_mini_renderer(code, host_renderer)
    if youtube_single_pin:
        patch_existing_main = patches["dist/main.js"]
        patches["dist/main.js"] = lambda code: patch_youtube_single_pin(patch_existing_main(code))
    if version:
        patches["package.json"] = lambda code: replace_once(
            code, '"version": "0.18.3"', f'"version": "{version}"'
        )
    for path, item in sorted(entries(header["files"]), key=lambda entry: int(entry[1]["offset"])):
        start = base + int(item["offset"])
        data = asar[start:start + item["size"]]
        if path in patches:
            data = patches[path](data.decode()).encode()
            integrity = item["integrity"]
            block_size = integrity["blockSize"]
            integrity["hash"] = hashlib.sha256(data).hexdigest()
            integrity["blocks"] = [
                hashlib.sha256(data[i:i + block_size]).hexdigest()
                for i in range(0, len(data), block_size)
            ]
        item["offset"] = str(len(body))
        item["size"] = len(data)
        body.extend(data)
    if hls_library is not None:
        data = hls_library.encode()
        digest = hashlib.sha256(data).hexdigest()
        header["files"]["dist"]["files"]["renderer"]["files"]["hls.js"] = {
            "size": len(data), "offset": str(len(body)),
            "integrity": {"algorithm": "SHA256", "hash": digest, "blockSize": 4194304, "blocks": [digest]},
        }
        body.extend(data)
    encoded = json.dumps(header, separators=(",", ":"), ensure_ascii=False).encode()
    padded_length = (len(encoded) + 3) & ~3
    prefix = struct.pack("<4I", 4, 8 + padded_length, 4 + padded_length, len(encoded))
    return prefix + encoded.ljust(padded_length, b"\0") + body


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", help="Set the app version and use the updater's ZIP filename")
    parser.add_argument("--ui-fix", action="store_true", help="Include all interface and playback updates")
    parser.add_argument("--youtube-single-pin", action="store_true",
                        help="Set tiny once before loading a YouTube track and log quality changes")
    args = parser.parse_args()
    version = args.version
    if version and not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
        parser.error("--version must have the form major.minor.patch")
    output = ROOT / f"KabanMusic-{version}-win.zip" if version else OUTPUT
    if args.ui_fix and not version:
        parser.error("--ui-fix requires --version")
    if args.youtube_single_pin and not args.ui_fix:
        parser.error("--youtube-single-pin requires --ui-fix")
    temporary = output.with_suffix(".tmp")
    with zipfile.ZipFile(ORIGINAL) as source, zipfile.ZipFile(temporary, "x") as target:
        for item in source.infolist():
            if item.filename == "resources/app.asar":
                target.writestr(item, patch_asar(source.read(item), version, args.ui_fix,
                                                args.youtube_single_pin))
            else:
                with source.open(item) as src, target.open(item, "w") as dst:
                    shutil.copyfileobj(src, dst)
    temporary.replace(output)


if __name__ == "__main__":
    main()
