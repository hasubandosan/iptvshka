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


async def resolve_search(entry, pool, session, sem, check_cfg) -> tuple[Channel | None, int]:
    """Ищет канал по названию в пуле источников, возвращает первый рабочий вариант."""
    patterns = as_list(entry.get("match")) or ["=" + entry["name"]]
    candidates = [c for c in pool if name_matches(c.name, patterns)]
    # точное совпадение названия — вперёд (сортировка стабильная, порядок источников сохраняется)
    target = squash(entry["name"], True)
    candidates.sort(key=lambda c: squash(c.name, True) != target)
    candidates = candidates[: check_cfg.get("max_candidates", 8)]

    for cand in candidates:
        if not check_cfg.get("enabled", True):
            return cand, len(candidates)
        async with sem:
            if await is_alive(session, cand.url, check_cfg):
                return cand, len(candidates)
    return None, len(candidates)


# --------------------------------------------------------------------------- #
#  Главная логика
# --------------------------------------------------------------------------- #
async def main(config_path: str) -> int:
    started = time.time()
    cfg = json.loads(Path(config_path).read_text(encoding="utf-8"))
    check_cfg = cfg.get("check", {})

    entries = json.loads(Path(cfg.get("channels_file", "channels.json")).read_text(encoding="utf-8"))["channels"]
    out_path = Path(cfg.get("output", "playlist.m3u"))

    # Прошлая версия плейлиста — запасной вариант, если свежий поток не найден
    previous: dict[str, Channel] = {}
    if out_path.exists():
        _, prev_channels = parse_m3u(out_path.read_text(encoding="utf-8"))
        previous = {squash(c.name): c for c in prev_channels}

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

        async def handle(entry: dict) -> tuple[Channel | None, str]:
            if entry.get("url"):
                url, alive = await resolve_pinned(entry, session, sem, check_cfg)
                return build_channel(entry, url), ("ok" if alive else "pinned_dead")
            src, n = await resolve_search(entry, pool, session, sem, check_cfg)
            if src:
                return build_channel(entry, src.url, src), "ok"
            old = previous.get(squash(entry["name"]))
            if old:
                return build_channel(entry, old.url, old), "kept_old"
            return None, ("not_found" if n == 0 else "all_dead")

        results = await asyncio.gather(*(handle(e) for e in entries))

    # 3. Отчёт и запись
    labels = {
        "ok": "OK",
        "pinned_dead": "ВНИМАНИЕ: ваша ссылка не отвечает (оставлена как есть)",
        "kept_old": "ВНИМАНИЕ: свежий поток не найден, оставлена ссылка из прошлого плейлиста",
        "not_found": "НЕ НАЙДЕН в источниках (проверьте название или добавьте источник)",
        "all_dead": "НАЙДЕН, но все потоки не отвечают",
    }
    result: list[Channel] = []
    problems = 0
    for entry, (channel, status) in zip(entries, results):
        print(f"  [{labels[status]}] {entry['name']}")
        if status != "ok":
            problems += 1
        if channel:
            result.append(channel)

    if not result:
        print("! Итоговый плейлист пуст. Файл НЕ перезаписан.")
        return 1

    print("[3/3] Запись файла...")
    body = "\n".join(channel_to_m3u(c) for c in result)
    out_path.write_text(f"{out_header}\n{body}\n", encoding="utf-8")
    print(f"Готово: {len(result)}/{len(entries)} каналов, проблем: {problems} -> {out_path} ({time.time() - started:.0f} c)")
    return 0


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else "config.json"
    sys.exit(asyncio.run(main(path)))
