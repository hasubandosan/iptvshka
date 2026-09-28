#!/usr/bin/env python3
"""
IPTV playlist updater.

Что делает:
  1. Скачивает m3u/m3u8 плейлисты из config.json -> "sources".
  2. Парсит их, сохраняя ВСЕ атрибуты #EXTINF (tvg-id, tvg-name, tvg-logo, group-title и любые другие)
     и дополнительные строки (#EXTVLCOPT, #KODIPROP и т.п.).
  3. Фильтрует по group-title / названию.
  4. Убирает дубликаты по названию (и по одинаковым URL).
     Для каждого названия берётся ПЕРВЫЙ РАБОТАЮЩИЙ вариант потока.
  5. Асинхронно проверяет потоки (таймаут из конфига).
  6. Записывает результат в playlist.m3u.

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


# --------------------------------------------------------------------------- #
#  Модель данных
# --------------------------------------------------------------------------- #
@dataclass
class Channel:
    name: str                                   # отображаемое имя (после запятой в #EXTINF)
    url: str                                    # ссылка на поток
    attrs: "OrderedDict[str, str]" = field(default_factory=OrderedDict)  # tvg-id, tvg-logo, ...
    extra: list = field(default_factory=list)   # доп. строки между #EXTINF и URL

    @property
    def group(self) -> str:
        return self.attrs.get("group-title", "")

    @property
    def key(self) -> str:
        """Ключ для поиска дубликатов: имя без регистра и лишних пробелов."""
        return re.sub(r"\s+", " ", self.name).strip().casefold()


# --------------------------------------------------------------------------- #
#  Парсинг M3U
# --------------------------------------------------------------------------- #
def split_extinf(line: str) -> tuple[str, str]:
    """
    Делит '#EXTINF:-1 a="x, y" b="z",Имя' на (часть_с_атрибутами, имя).
    Запятая-разделитель — первая запятая ВНЕ кавычек (в group-title запятые бывают).
    """
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

    pending: Channel | None = None   # канал, для которого ждём строку с URL
    pending_group: str | None = None  # значение из #EXTGRP

    for raw in text.splitlines():
        line = raw.strip().lstrip("\ufeff")
        if not line:
            continue

        if line.upper().startswith("#EXTM3U"):
            header = line  # сохраняем, например, url-tvg="..." / x-tvg-url="..."
            continue

        if line.startswith("#EXTINF"):
            head, name = split_extinf(line)
            attrs = OrderedDict(ATTR_RE.findall(head))
            # если имени после запятой нет — берём tvg-name
            name = name or attrs.get("tvg-name", "") or "Unnamed"
            pending = Channel(name=name, url="", attrs=attrs)
            pending_group = None
            continue

        if line.startswith("#EXTGRP:"):
            pending_group = line.split(":", 1)[1].strip()
            continue

        if line.startswith("#"):
            # прочие директивы (#EXTVLCOPT, #KODIPROP...) — сохраняем как есть
            if pending is not None:
                pending.extra.append(line)
            continue

        # Иначе это строка с URL
        if pending is not None:
            pending.url = line
            if pending_group and not pending.attrs.get("group-title"):
                pending.attrs["group-title"] = pending_group
            channels.append(pending)
            pending = None

    return header, channels


def channel_to_m3u(ch: Channel) -> str:
    """Собирает канал обратно в текст M3U, сохраняя порядок атрибутов."""
    attrs = " ".join(f'{k}="{v.replace(chr(34), chr(39))}"' for k, v in ch.attrs.items())
    lines = [f"#EXTINF:-1 {attrs},{ch.name}" if attrs else f"#EXTINF:-1,{ch.name}"]
    lines.extend(ch.extra)
    lines.append(ch.url)
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
#  Загрузка источников
# --------------------------------------------------------------------------- #
async def fetch_source(session: aiohttp.ClientSession, url: str) -> str | None:
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=60)) as resp:
            resp.raise_for_status()
            return await resp.text(errors="replace")
    except Exception as exc:  # один упавший источник не должен ломать весь процесс
        print(f"  ! Не удалось скачать {url}: {exc}")
        return None


# --------------------------------------------------------------------------- #
#  Фильтры
# --------------------------------------------------------------------------- #
def passes_filters(ch: Channel, flt: dict) -> bool:
    group = ch.group.casefold()
    name = ch.name.casefold()

    include = [g.casefold() for g in flt.get("include_groups", [])]
    exclude = [g.casefold() for g in flt.get("exclude_groups", [])]
    bad_names = [n.casefold() for n in flt.get("exclude_names", [])]

    if include and not any(g in group for g in include):
        return False
    if any(g in group for g in exclude):
        return False
    if any(n in name for n in bad_names):
        return False
    return True


# --------------------------------------------------------------------------- #
#  Проверка потоков
# --------------------------------------------------------------------------- #
async def is_alive(session: aiohttp.ClientSession, url: str, cfg: dict) -> bool:
    """
    Проверка одного потока. HEAD многие IPTV-серверы не поддерживают,
    поэтому делаем GET и читаем только первые байты (поток целиком не качаем).
    """
    if not url.lower().startswith(("http://", "https://")):
        return cfg.get("keep_non_http", True)  # rtmp://, udp:// и т.д. проверить нельзя

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
                # "мягкие" ошибки: сервер отдаёт 200, но это HTML-страница
                if "text/html" in ctype and not is_hls:
                    continue
                # HLS-плейлист должен начинаться с #EXTM3U
                if is_hls and cfg.get("strict_hls", True) and b"#EXTM3U" not in chunk:
                    continue
                return True
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError, ValueError):
            continue
    return False


async def pick_alive(
    candidates: list[Channel],
    session: aiohttp.ClientSession,
    sem: asyncio.Semaphore,
    cfg: dict,
    progress: dict,
) -> Channel | None:
    """Из вариантов одного канала возвращает первый рабочий (по порядку источников)."""
    result = None
    for ch in candidates:
        async with sem:
            ok = await is_alive(session, ch.url, cfg)
        if ok:
            result = ch
            break
    progress["done"] += 1
    if progress["done"] % 250 == 0:
        print(f"  ... проверено {progress['done']}/{progress['total']}")
    return result


# --------------------------------------------------------------------------- #
#  Главная логика
# --------------------------------------------------------------------------- #
async def main(config_path: str) -> int:
    started = time.time()
    cfg = json.loads(Path(config_path).read_text(encoding="utf-8"))
    check_cfg = cfg.get("check", {})
    headers = {"User-Agent": check_cfg.get("user_agent", "Mozilla/5.0")}

    # ssl=False: у многих IPTV-серверов кривые/просроченные сертификаты
    connector = aiohttp.TCPConnector(limit=check_cfg.get("concurrency", 100), ssl=False)

    async with aiohttp.ClientSession(headers=headers, connector=connector) as session:
        # 1. Скачиваем источники параллельно
        print(f"[1/4] Скачивание источников ({len(cfg['sources'])})...")
        texts = await asyncio.gather(*(fetch_source(session, u) for u in cfg["sources"]))

        all_channels: list[Channel] = []
        out_header = "#EXTM3U"
        for url, text in zip(cfg["sources"], texts):
            if not text:
                continue
            header, chans = parse_m3u(text)
            print(f"  + {url}: {len(chans)} каналов")
            all_channels.extend(chans)
            # берём заголовок с EPG (url-tvg / x-tvg-url) от первого источника, где он есть
            if out_header == "#EXTM3U" and header.strip() != "#EXTM3U":
                out_header = header

        total_raw = len(all_channels)

        # 2. Фильтры + удаление дублей по URL
        print("[2/4] Фильтрация...")
        seen_urls: set[str] = set()
        filtered: list[Channel] = []
        for ch in all_channels:
            if not ch.url or ch.url in seen_urls:
                continue
            if not passes_filters(ch, cfg.get("filters", {})):
                continue
            seen_urls.add(ch.url)
            filtered.append(ch)

        # 3. Группируем по названию (дубликаты) — порядок появления сохраняется
        groups: "OrderedDict[str, list[Channel]]" = OrderedDict()
        for ch in filtered:
            groups.setdefault(ch.key, []).append(ch)
        print(f"  После фильтров: {len(filtered)} потоков, уникальных названий: {len(groups)}")

        # 4. Проверка доступности
        if check_cfg.get("enabled", True):
            print(f"[3/4] Проверка потоков (таймаут {check_cfg.get('timeout', 4)} c)...")
            sem = asyncio.Semaphore(check_cfg.get("concurrency", 100))
            progress = {"done": 0, "total": len(groups)}
            picked = await asyncio.gather(
                *(pick_alive(c, session, sem, check_cfg, progress) for c in groups.values())
            )
            result = [ch for ch in picked if ch is not None]
        else:
            print("[3/4] Проверка отключена — берём первый вариант каждого канала")
            result = [c[0] for c in groups.values()]

    # Сортировка по группам (по желанию)
    if cfg.get("sort_by_group"):
        result.sort(key=lambda c: (c.group.casefold(), c.name.casefold()))

    # Защита: если почти всё "умерло" (например, упала сеть) — не затираем старый плейлист
    if len(result) < cfg.get("min_channels", 1):
        print(f"! Итог слишком мал ({len(result)} < min_channels). Файл НЕ перезаписан.")
        return 1

    # 5. Запись
    print("[4/4] Запись файла...")
    out_path = Path(cfg.get("output", "playlist.m3u"))
    body = "\n".join(channel_to_m3u(ch) for ch in result)
    out_path.write_text(f"{out_header}\n{body}\n", encoding="utf-8")

    print(
        f"Готово: {len(result)} рабочих каналов из {total_raw} исходных "
        f"-> {out_path} ({time.time() - started:.0f} c)"
    )
    return 0


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else "config.json"
    sys.exit(asyncio.run(main(path)))
