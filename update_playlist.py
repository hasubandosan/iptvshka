#!/usr/bin/env python3
"""
IPTV playlist updater — режим «мои каналы».

Вы описываете нужные каналы в channels.json. Для каждого канала скрипт:
  * если указан "url"  -> берёт вашу жёсткую ссылку (можно несколько — как запасные);
  * если "url" нет     -> ищет канал по названию в плейлистах-источниках (config.json -> sources),
                          проверяет потоки и берёт первый рабочий.
Итоговый playlist.m3u содержит ТОЛЬКО ваши каналы, в том порядке, как они записаны в channels.json.

Если для канала не нашлось рабочего потока, остаётся ссылка из предыдущей версии playlist.m3u
(чтобы канал не пропадал из-за временного сбоя источника).

Запуск:  python update_playlist.py [путь_к_config.json]
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
import time
from urllib.parse import urljoin, urlparse
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path

import aiohttp

# Ищет пары ключ="значение" внутри строки #EXTINF
ATTR_RE = re.compile(r'([\w\-]+)="([^"]*)"')
# Пометки качества, которые игнорируются при точном сравнении названий
QUALITY_RE = re.compile(r"\b(hd|sd|fhd|uhd|4k)\b", re.IGNORECASE)


# --------------------------------------------------------------------------- #
#  Модель данных
# --------------------------------------------------------------------------- #
@dataclass
class Channel:
    name: str
    url: str
    attrs: "OrderedDict[str, str]" = field(default_factory=OrderedDict)  # tvg-id, tvg-logo, ...
    extra: list = field(default_factory=list)  # доп. строки (#EXTVLCOPT и т.п.)


# --------------------------------------------------------------------------- #
#  Сравнение названий
# --------------------------------------------------------------------------- #
def squash(text: str, drop_quality: bool = False) -> str:
    """'Муз-ТВ HD' -> 'музтв' (при drop_quality=True). Убирает регистр, пробелы, знаки, 'ё'->'е'."""
    text = text.casefold().replace("ё", "е")
    if drop_quality:
        text = QUALITY_RE.sub("", text)
    return re.sub(r"[\W_]+", "", text)


def name_matches(name: str, patterns: list[str]) -> bool:
    """
    'ТНТ'  — название СОДЕРЖИТ это (найдёт и 'ТНТ4').
    '=ТНТ' — название ТОЧНО равно (игнорируя регистр, пробелы, дефисы, HD/SD).
    """
    for p in patterns:
        if p.startswith("="):
            if squash(name, True) == squash(p[1:], True):
                return True
        elif squash(p) in squash(name):
            return True
    return False


def is_blocked(url: str, patterns: list[str]) -> bool:
    """URL содержит любую из подстрок (без учёта регистра) -> источник заблокирован по региону."""
    low = url.lower()
    return any(p.lower() in low for p in patterns if p)


def is_adult(ch: Channel, entry: dict, adult_cfg: dict) -> bool:
    """Канал для взрослых: entry['adult']=true или group-title входит в adult.groups."""
    if not adult_cfg.get("enabled", False):
        return False
    if entry.get("adult") is True:
        return True
    if entry.get("adult") is False:
        return False
    group = squash(ch.attrs.get("group-title", ""))
    return any(squash(g) == group for g in as_list(adult_cfg.get("groups", ["Adults"])))


def sort_channels(pairs: list, cfg: dict) -> list:
    """
    pairs = [(entry, channel), ...] в порядке channels.json.
    sort: manual (как в channels.json) | name | group | group_name
    group_order: список групп в нужном порядке; остальные идут после них по алфавиту.
    Внутри одной группы порядок как в channels.json (для group) или по названию (для group_name).
    """
    mode = cfg.get("sort", "manual")
    if mode == "manual":
        return pairs
    order = {squash(g): i for i, g in enumerate(as_list(cfg.get("group_order")))}

    def gkey(ch: Channel):
        g = squash(ch.attrs.get("group-title", ""))
        return (order.get(g, len(order)), g)

    if mode == "name":
        return sorted(pairs, key=lambda p: squash(p[1].name))
    if mode == "group":
        return sorted(pairs, key=lambda p: gkey(p[1]))  # stable: внутри группы порядок сохраняется
    if mode == "group_name":
        return sorted(pairs, key=lambda p: (gkey(p[1]), squash(p[1].name)))
    print(f"  ! Неизвестный режим sort='{mode}', использую manual")
    return pairs


def as_list(value) -> list:
    if value is None or value == "":
        return []
    return value if isinstance(value, list) else [value]


# --------------------------------------------------------------------------- #
#  Парсинг / запись M3U
# --------------------------------------------------------------------------- #
def split_extinf(line: str) -> tuple[str, str]:
    """Делит '#EXTINF:-1 a="x, y",Имя' по первой запятой ВНЕ кавычек."""
    in_quotes = False
    for i, ch in enumerate(line):
        if ch == '"':
            in_quotes = not in_quotes
        elif ch == "," and not in_quotes:
            return line[:i], line[i + 1:].strip()
    return line, ""


def parse_m3u(text: str) -> tuple[str, list[Channel]]:
    """Возвращает (строка_заголовка #EXTM3U, список каналов)."""
    header = "#EXTM3U"
    channels: list[Channel] = []
    pending: Channel | None = None
    pending_group: str | None = None

    for raw in text.splitlines():
        line = raw.strip().lstrip("\ufeff")
        if not line:
            continue
        if line.upper().startswith("#EXTM3U"):
            header = line
            continue
        if line.startswith("#EXTINF"):
            head, name = split_extinf(line)
            attrs = OrderedDict(ATTR_RE.findall(head))
            pending = Channel(name=name or attrs.get("tvg-name", "") or "Unnamed", url="", attrs=attrs)
            pending_group = None
            continue
        if line.startswith("#EXTGRP:"):
            pending_group = line.split(":", 1)[1].strip()
            continue
        if line.startswith("#"):
            if pending is not None:
                pending.extra.append(line)
            continue
        if pending is not None:  # строка с URL
            pending.url = line
            if pending_group and not pending.attrs.get("group-title"):
                pending.attrs["group-title"] = pending_group
            channels.append(pending)
            pending = None
    return header, channels


def channel_to_m3u(ch: Channel) -> str:
    attrs = " ".join(f'{k}="{v.replace(chr(34), chr(39))}"' for k, v in ch.attrs.items())
    lines = [f"#EXTINF:-1 {attrs},{ch.name}" if attrs else f"#EXTINF:-1,{ch.name}"]
    lines.extend(ch.extra)
    lines.append(ch.url)
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
#  Сеть
# --------------------------------------------------------------------------- #
async def fetch_source(session: aiohttp.ClientSession, url: str) -> str | None:
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=60)) as resp:
            resp.raise_for_status()
            return await resp.text(errors="replace")
    except Exception as exc:
        print(f"  ! Не удалось скачать {url}: {exc}")
        return None


async def is_alive(session: aiohttp.ClientSession, url: str, cfg: dict) -> bool:
    """GET + чтение первых байт (HEAD многие IPTV-серверы не поддерживают)."""
    if not url.lower().startswith(("http://", "https://")):
        return cfg.get("keep_non_http", True)  # rtmp://, udp:// проверить нельзя

    is_hls = ".m3u8" in url.lower()
    timeout = aiohttp.ClientTimeout(total=cfg.get("timeout", 4))

    for _ in range(1 + cfg.get("retries", 1)):
        try:
            async with session.get(url, timeout=timeout, allow_redirects=True) as resp:
                if resp.status >= 400:
                    continue
                ctype = resp.headers.get("Content-Type", "").lower()
                chunk = await resp.content.read(2048)
                if not chunk:
                    continue
                if "text/html" in ctype and not is_hls:  # «мягкая» ошибка: 200, но страница
                    continue
                if is_hls and cfg.get("strict_hls", True) and b"#EXTM3U" not in chunk:
                    continue
                return True
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError, ValueError):
            continue
    return False


# --------------------------------------------------------------------------- #
#  Замер скорости потока (HLS)
# --------------------------------------------------------------------------- #
def parse_hls(text: str, base: str) -> tuple[list, list, bool]:
    """Возвращает (варианты [(bandwidth, url)], сегменты [(длительность, url)], есть_byterange)."""
    variants, segments, byterange = [], [], False
    pending_bw, pending_dur = None, None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#EXT-X-STREAM-INF"):
            m = re.search(r"BANDWIDTH=(\d+)", line)
            pending_bw = int(m.group(1)) if m else 0
        elif line.startswith("#EXTINF"):
            m = re.match(r"#EXTINF:([\d.]+)", line)
            pending_dur = float(m.group(1)) if m else None
        elif line.startswith("#EXT-X-BYTERANGE"):
            byterange = True
        elif not line.startswith("#"):
            if pending_bw is not None:
                variants.append((pending_bw, urljoin(base, line)))
                pending_bw = None
            elif pending_dur:
                segments.append((pending_dur, urljoin(base, line)))
            pending_dur = None
    return variants, segments, byterange


async def probe_speed(session: aiohttp.ClientSession, url: str, scfg: dict) -> dict | None:
    """
    Качает несколько последних сегментов HLS-потока и меряет реальную скорость загрузки.
      mbps  — скорость скачивания, Мбит/с
      ratio — запас по скорости: секунд видео скачано за секунду. <1 = будет буферизация
      kbps  — фактический битрейт потока
    None — измерить нельзя (не HLS, нестандартный плейлист, ошибка).
    """
    if ".m3u8" not in url.lower() or not url.lower().startswith(("http://", "https://")):
        return None
    timeout = aiohttp.ClientTimeout(total=scfg.get("timeout", 10))
    try:
        async def get_text(u):
            async with session.get(u, timeout=timeout, allow_redirects=True) as r:
                r.raise_for_status()
                return str(r.url), await r.text(errors="replace")

        base, text = await get_text(url)
        variants, segments, byterange = parse_hls(text, base)
        if variants:  # мастер-плейлист: берём самый качественный вариант в пределах лимита
            cap = scfg.get("max_variant_mbps", 8) * 1_000_000
            fit = [v for v in variants if v[0] <= cap] or [min(variants)]
            base, text = await get_text(max(fit)[1])
            _, segments, byterange = parse_hls(text, base)
        if not segments or byterange:
            return None
        n = scfg.get("segments", 2)
        # самый свежий сегмент может быть ещё не дописан — пропускаем его, если есть выбор
        chosen = segments[-(n + 1):-1] if len(segments) > n else segments[-n:]
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError, ValueError):
        return None

    total_bytes, total_dur = 0, 0.0
    started = time.perf_counter()
    try:
        for dur, seg_url in chosen:
            async with session.get(seg_url, timeout=timeout, allow_redirects=True) as r:
                r.raise_for_status()
                async for part in r.content.iter_chunked(65536):
                    total_bytes += len(part)
            total_dur += dur
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
        pass  # недокачанный сегмент в длительность не входит -> ratio получится низким
    elapsed = max(time.perf_counter() - started, 1e-3)
    if total_bytes == 0:
        return None
    return {
        "mbps": total_bytes * 8 / elapsed / 1e6,
        "ratio": total_dur / elapsed,
        "kbps": (total_bytes * 8 / total_dur / 1000) if total_dur else 0.0,
    }


def host_of(url: str) -> str:
    return urlparse(url).netloc or url[:30]


async def pick_best(cands, session, sem, probe_sem, check_cfg, speed_cfg) -> tuple[Channel | None, str]:
    """
    Из кандидатов оставляет живые, замеряет скорость и выбирает лучший.
      1) «плавные» (ratio >= min_ratio): лучший по rank_by (speed = скорость, quality = битрейт);
         все, чей результат в пределах tolerance от лучшего, считаются равными -> побеждает тот,
         кто выше по приоритету (точное название, порядок источников). Так плейлист не «скачет» из-за шума.
      2) не поддающиеся замеру (не HLS) — после плавных;
      3) медленные (ratio < min_ratio) — в самом конце, если больше ничего нет.
    """
    async def alive(c):
        async with sem:
            return await is_alive(session, c.url, check_cfg)

    flags = await asyncio.gather(*(alive(c) for c in cands))
    live = [c for c, ok in zip(cands, flags) if ok]
    if not live:
        return None, ""
    top = live[: speed_cfg.get("max_probe", 4)]
    if len(top) == 1:
        return top[0], ""

    async def probe(c):
        async with probe_sem:
            return await probe_speed(session, c.url, speed_cfg)

    stats = await asyncio.gather(*(probe(c) for c in top))
    min_ratio = speed_cfg.get("min_ratio", 1.3)
    tol = speed_cfg.get("tolerance", 0.15)
    key = "kbps" if speed_cfg.get("rank_by", "speed") == "quality" else "mbps"

    smooth = [(c, r) for c, r in zip(top, stats) if r and r["ratio"] >= min_ratio]
    unknown = [c for c, r in zip(top, stats) if r is None]
    slow = [(c, r) for c, r in zip(top, stats) if r and r["ratio"] < min_ratio]

    best_r = None
    if smooth:
        best = max(r[key] for _, r in smooth)
        winner, best_r = next((c, r) for c, r in smooth if r[key] >= best * (1 - tol))
    elif unknown:
        winner = unknown[0]
    else:
        winner, best_r = max(slow, key=lambda cr: cr[1]["mbps"])

    parts = [f"{host_of(c.url)} " + (f"{r['mbps']:.1f}" if r else "н/д") for c, r in zip(top, stats)]
    head = (f"{best_r['mbps']:.1f} Мбит/с, запас ×{best_r['ratio']:.1f}, поток ~{best_r['kbps'] / 1000:.1f} Мбит/с"
            if best_r else "скорость не измерена")
    return winner, f"{head} | выбран {host_of(winner.url)} из: " + "; ".join(parts)


# --------------------------------------------------------------------------- #
#  Разрешение записей из channels.json
# --------------------------------------------------------------------------- #
def build_channel(entry: dict, url: str, src: Channel | None = None) -> Channel:
    """Собирает итоговый канал: атрибуты найденного источника + ваши переопределения."""
    attrs: "OrderedDict[str, str]" = OrderedDict(src.attrs) if src else OrderedDict()
    overrides = {
        "tvg-id": entry.get("tvg_id"),
        "tvg-name": entry.get("tvg_name"),
        "tvg-logo": entry.get("logo"),
        "group-title": entry.get("group"),
    }
    for key, value in overrides.items():
        if value:
            attrs[key] = value
    if not src:
        attrs.setdefault("tvg-name", entry["name"])
    extra = (list(src.extra) if src else []) + as_list(entry.get("extra"))
    return Channel(name=entry["name"], url=url, attrs=attrs, extra=list(dict.fromkeys(extra)))


async def resolve_pinned(entry, session, sem, check_cfg) -> tuple[str, bool]:
    """Жёсткая ссылка: берём первую живую из списка; если все мертвы — всё равно первую."""
    urls = as_list(entry["url"])
    if not check_cfg.get("enabled", True):
        return urls[0], True
    for url in urls:
        async with sem:
            if await is_alive(session, url, check_cfg):
                return url, True
    return urls[0], False


async def resolve_search(entry, pool, session, sem, probe_sem, check_cfg, speed_cfg, blocked):
    """Ищет канал в пуле источников. Возвращает (канал | None, число кандидатов, заметка для лога)."""
    patterns = as_list(entry.get("match")) or ["=" + entry["name"]]
    bad = blocked + as_list(entry.get("exclude"))  # глобальный + свой список для канала
    candidates = [c for c in pool if name_matches(c.name, patterns) and not is_blocked(c.url, bad)]
    # точное совпадение названия — вперёд (сортировка стабильная, порядок источников сохраняется)
    target = squash(entry["name"], True)
    candidates.sort(key=lambda c: squash(c.name, True) != target)
    candidates = candidates[: check_cfg.get("max_candidates", 8)]

    if not check_cfg.get("enabled", True):
        return (candidates[0] if candidates else None), len(candidates), ""

    if speed_cfg.get("enabled", False):
        winner, note = await pick_best(candidates, session, sem, probe_sem, check_cfg, speed_cfg)
        return winner, len(candidates), note

    for cand in candidates:  # без замера скорости: первый живой по приоритету
        async with sem:
            if await is_alive(session, cand.url, check_cfg):
                return cand, len(candidates), ""
    return None, len(candidates), ""


# --------------------------------------------------------------------------- #
#  Главная логика
# --------------------------------------------------------------------------- #
async def main(config_path: str) -> int:
    started = time.time()
    cfg = json.loads(Path(config_path).read_text(encoding="utf-8"))
    check_cfg = cfg.get("check", {})
    speed_cfg = cfg.get("speed", {})
    blocked = as_list(cfg.get("blocked"))  # хосты/подстроки, недоступные из вашего региона

    entries = json.loads(Path(cfg.get("channels_file", "channels.json")).read_text(encoding="utf-8"))["channels"]
    out_path = Path(cfg.get("output", "playlist.m3u"))
    adult_cfg = cfg.get("adult", {})
    adult_path = Path(adult_cfg.get("output", "playlist_adult.m3u"))

    # Прошлые версии плейлистов (обычный + для взрослых) — запасной вариант, если свежий поток не найден
    previous: dict[str, Channel] = {}
    for prev_path in (out_path, adult_path):
        if prev_path.exists():
            _, prev_channels = parse_m3u(prev_path.read_text(encoding="utf-8"))
            previous.update({squash(c.name): c for c in prev_channels})

    headers = {"User-Agent": check_cfg.get("user_agent", "Mozilla/5.0")}
    connector = aiohttp.TCPConnector(limit=check_cfg.get("concurrency", 100), ssl=False)

    async with aiohttp.ClientSession(headers=headers, connector=connector) as session:
        # 1. Источники нужны, только если есть каналы без жёсткой ссылки
        pool: list[Channel] = []
        out_header = "#EXTM3U"
        if any(not e.get("url") for e in entries):
            print(f"[1/3] Скачивание источников ({len(cfg['sources'])})...")
            texts = await asyncio.gather(*(fetch_source(session, u) for u in cfg["sources"]))
            seen_urls: set[str] = set()
            for url, text in zip(cfg["sources"], texts):
                if not text:
                    continue
                header, chans = parse_m3u(text)
                print(f"  + {url}: {len(chans)} каналов")
                for ch in chans:
                    if ch.url and ch.url not in seen_urls:
                        seen_urls.add(ch.url)
                        pool.append(ch)
                if out_header == "#EXTM3U" and header.strip() != "#EXTM3U":
                    out_header = header  # заголовок с EPG от первого источника, где он есть
        if cfg.get("epg_url"):
            out_header = f'#EXTM3U url-tvg="{cfg["epg_url"]}"'

        # 2. Разрешаем все записи параллельно (порядок результата = порядок в channels.json)
        print(f"[2/3] Поиск и проверка потоков для {len(entries)} каналов...")
        sem = asyncio.Semaphore(check_cfg.get("concurrency", 100))
        # замеры скорости идут по нескольку штук за раз, иначе они мешают друг другу
        probe_sem = asyncio.Semaphore(speed_cfg.get("concurrency", 3))
        notes: dict[str, str] = {}

        async def handle(entry: dict) -> tuple[Channel | None, str]:
            if entry.get("url"):
                url, alive = await resolve_pinned(entry, session, sem, check_cfg)
                if is_blocked(url, blocked):
                    return build_channel(entry, url), "pinned_blocked"
                return build_channel(entry, url), ("ok" if alive else "pinned_dead")
            src, n, note = await resolve_search(entry, pool, session, sem, probe_sem, check_cfg, speed_cfg, blocked)
            if note:
                notes[entry["name"]] = note
            if src:
                return build_channel(entry, src.url, src), "ok"
            old = previous.get(squash(entry["name"]))
            if old and not is_blocked(old.url, blocked + as_list(entry.get("exclude"))):
                return build_channel(entry, old.url, old), "kept_old"
            return None, ("not_found" if n == 0 else "all_dead")

        results = await asyncio.gather(*(handle(e) for e in entries))

    # 3. Отчёт и запись
    labels = {
        "ok": "OK",
        "pinned_blocked": "ВНИМАНИЕ: ваша ссылка ведёт на заблокированный хост (оставлена как есть)",
        "pinned_dead": "ВНИМАНИЕ: ваша ссылка не отвечает (оставлена как есть)",
        "kept_old": "ВНИМАНИЕ: свежий поток не найден, оставлена ссылка из прошлого плейлиста",
        "not_found": "НЕ НАЙДЕН в источниках (проверьте название или добавьте источник)",
        "all_dead": "НАЙДЕН, но все потоки не отвечают",
    }
    pairs: list = []
    problems = 0
    for entry, (channel, status) in zip(entries, results):
        note = notes.get(entry["name"])
        print(f"  [{labels[status]}] {entry['name']}" + (f" — {note}" if note else ""))
        if status != "ok":
            problems += 1
        if channel:
            pairs.append((entry, channel))

    if not pairs:
        print("! Итоговый плейлист пуст. Файлы НЕ перезаписаны.")
        return 1

    # Делим на обычные и «для взрослых», затем сортируем каждый список
    adult_pairs = [p for p in pairs if is_adult(p[1], p[0], adult_cfg)]
    main_pairs = [p for p in pairs if p not in adult_pairs]
    main_pairs = sort_channels(main_pairs, cfg)
    adult_pairs = sort_channels(adult_pairs, cfg)

    print("[3/3] Запись файлов...")
    body = "\n".join(channel_to_m3u(c) for _, c in main_pairs)
    out_path.write_text(f"{out_header}\n{body}\n" if body else f"{out_header}\n", encoding="utf-8")
    print(f"  {out_path}: {len(main_pairs)} каналов")
    if adult_cfg.get("enabled", False):
        body = "\n".join(channel_to_m3u(c) for _, c in adult_pairs)
        adult_path.write_text(f"{out_header}\n{body}\n" if body else f"{out_header}\n", encoding="utf-8")
        print(f"  {adult_path}: {len(adult_pairs)} каналов")
    print(f"Готово: {len(pairs)}/{len(entries)} каналов, проблем: {problems} ({time.time() - started:.0f} c)")
    return 0


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else "config.json"
    sys.exit(asyncio.run(main(path)))
