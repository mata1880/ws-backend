"""
Gundam Card Game catalog scraper: official Japanese site (gundam-gcg.com/jp/cards/).

How the site is laid out (confirmed from saved copies of the real pages):
  * The card list page has one link per set:  data-val="615101" -> "Newtype Rising [GD01]"
  * A set's cards:  /jp/cards/?search=true&package=615101
      every card is  <li class="cardItem"><a data-src="detail.php?detailSearch=GD01-001"> ...
      parallels get their own id:  GD01-001_p1, GD01-001_p2
  * One card:  /jp/cards/detail.php?detailSearch=GD01-001
      .cardNo / .rarity / h1.cardName / .cardImage img, then labelled
      <dl class="dataBox"><dt class="dataTit">COST</dt><dd class="dataTxt">3</dd></dl> pairs,
      and the effect text in .cardDataRow.overview .dataTxt

For personal use only: requests are made one at a time with a pause between
them, and cards already filled in are skipped on later runs.
"""
import re
import time
from dataclasses import dataclass, field
from typing import List, Optional, Tuple
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

BASE = "https://www.gundam-gcg.com/jp/cards/"
HEADERS = {"User-Agent": "Mozilla/5.0 (personal collection tracker)"}


@dataclass
class GcgCard:
    detail_id: str                 # "GD01-001" or "GD01-001_p1" (unique; used as our card_number)
    card_number: str               # as printed: "GD01-001"
    name: str = ""
    rarity: Optional[str] = None
    image_url: Optional[str] = None
    level: Optional[str] = None
    cost: Optional[str] = None
    color: Optional[str] = None
    card_type: Optional[str] = None
    ap: Optional[str] = None
    hp: Optional[str] = None
    terrain: Optional[str] = None
    traits: List[str] = field(default_factory=list)
    link: Optional[str] = None
    source_title: Optional[str] = None
    set_name: Optional[str] = None
    text: Optional[str] = None


# ---------------------------------------------------------------- parsing (pure, testable)

def parse_sets(html: str) -> List[Tuple[str, str]]:
    """[(package_id, "Newtype Rising [GD01]"), ...] in site order, no duplicates."""
    out, seen = [], set()
    for pid, name in re.findall(r'js-selectBtn-package[^"]*"\s*data-val="(\d+)"[^>]*>([^<]+)<', html):
        if pid not in seen:
            seen.add(pid)
            out.append((pid, " ".join(name.split())))
    return out


def parse_card_ids(html: str) -> List[str]:
    """Detail ids of every card on a set page, in order, no duplicates."""
    out, seen = [], set()
    for cid in re.findall(r'data-src="detail\.php\?detailSearch=([^"&]+)"', html):
        if cid not in seen:
            seen.add(cid)
            out.append(cid)
    return out


def _txt(el) -> Optional[str]:
    if el is None:
        return None
    t = " ".join(el.get_text(" ", strip=True).split())
    return t or None


def parse_detail(html: str, detail_id: str) -> GcgCard:
    soup = BeautifulSoup(html, "html.parser")
    number = _txt(soup.select_one(".cardNo")) or detail_id.split("_")[0]
    rarity = _txt(soup.select_one(".rarity"))
    if rarity:
        rarity = rarity.replace(" ", "")   # official site writes "LR ++"; yuyu-tei writes "LR++"
    # Parallels (…_p1, _p2) share the printed number; shops call them LR+ / LR++.
    m = re.search(r"_p(\d+)$", detail_id)
    if m and rarity and "+" not in rarity:
        rarity = rarity + "+" * int(m.group(1))

    card = GcgCard(detail_id=detail_id, card_number=number,
                   name=_txt(soup.select_one(".cardName")) or "", rarity=rarity)

    img = soup.select_one(".cardImage img")
    if img and img.get("src"):
        card.image_url = urljoin(BASE + "detail.php", img["src"].strip())

    data = {}
    for dl in soup.select("dl.dataBox"):
        k, v = _txt(dl.select_one(".dataTit")), _txt(dl.select_one(".dataTxt"))
        if k:
            data[k] = v
    card.level, card.cost = data.get("Lv."), data.get("COST")
    card.color, card.card_type = data.get("色"), data.get("タイプ")
    card.ap, card.hp = data.get("AP"), data.get("HP")
    card.terrain, card.link = data.get("地形"), data.get("リンク")
    card.source_title, card.set_name = data.get("出典タイトル"), data.get("入手情報")
    if data.get("特徴"):
        card.traits = [t for t in re.split(r"\s+", data["特徴"]) if t]

    ov = soup.select_one(".cardDataRow.overview .dataTxt")
    if ov is not None:
        for br in ov.find_all("br"):
            br.replace_with("\n")
        lines = [" ".join(l.split()) for l in ov.get_text().split("\n")]
        card.text = "\n".join(l for l in lines if l) or None
    return card


def resolve_sets(sets: List[Tuple[str, str]], query: str) -> List[Tuple[str, str]]:
    """Match "GD01" / "st01" / a package id / part of a set name."""
    q = query.strip().lower()
    by_code = [s for s in sets if re.search(r"\[" + re.escape(q) + r"\]", s[1].lower())]
    if by_code:
        return by_code
    return [s for s in sets if s[0] == q or q in s[1].lower()]


# ---------------------------------------------------------------- network

def _get(session: requests.Session, url: str) -> str:
    r = session.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    r.encoding = "utf-8"
    return r.text


def fetch_sets(session: requests.Session) -> List[Tuple[str, str]]:
    return parse_sets(_get(session, BASE))


def fetch_card_ids(session: requests.Session, package_id: str) -> List[str]:
    return parse_card_ids(_get(session, f"{BASE}?search=true&package={package_id}"))


def fetch_detail(session: requests.Session, detail_id: str) -> GcgCard:
    return parse_detail(_get(session, f"{BASE}detail.php?detailSearch={detail_id}"), detail_id)
