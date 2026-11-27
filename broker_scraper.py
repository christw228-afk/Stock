from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

import requests
from bs4 import BeautifulSoup

MIRROR_BASES = [
    "https://fubon-ebrokerdj.fbs.com.tw",
    "https://djinfo.cathaysec.com.tw",
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    )
}


@dataclass
class BrokerSummaryRow:
    name: str
    buy_lots: int
    sell_lots: int
    net_lots: int
    volume_pct: float
    zco0_href: Optional[str] = None


@dataclass
class BrokerRangeReport:
    stock_id: str
    stock_name: str
    last_update_date: str
    buy_top: list[BrokerSummaryRow]
    sell_top: list[BrokerSummaryRow]


@dataclass
class BranchDailyRecord:
    date: str  # YYYY-MM-DD
    buy_lots: int
    sell_lots: int
    total_lots: int
    net_lots: int


@dataclass
class ChipFlowSummary:
    data_date: str = ""
    foreign_lots: int = 0
    foreign_chg: int = 0
    trust_lots: int = 0
    trust_chg: int = 0
    dealer_lots: int = 0
    dealer_chg: int = 0
    margin_balance: int = 0
    margin_chg: int = 0
    margin_available: bool = False
    margin_latest_date: str = ""
    margin_latest_chg: int = 0
    margin_pct_capital: float = 0.0
    short_balance: int = 0
    short_chg: int = 0
    short_available: bool = False


@dataclass
class BigHolderWeekly:
    latest_date: str
    big_holder_pct: float
    prev_big_holder_pct: float
    big_holder_diff: float
    concentration_pct: float
    concentration_diff: float


FOREIGN_BROKER_KEYWORDS = (
    "摩根",
    "高盛",
    "美林",
    "花旗",
    "瑞銀",
    "野村",
    "麥格理",
    "法銀巴黎",
    "匯豐",
    "德意志",
    "大和",
    "新加坡商",
    "港商",
    "美商",
    "瑞士信貸",
    "澳商",
    "日商",
)


def is_foreign_broker(name: str) -> bool:
    """Return True if the broker branch is a foreign institutional brokerage."""
    return any(kw in name for kw in FOREIGN_BROKER_KEYWORDS)


def _parse_int(text: str) -> int:
    cleaned = text.replace(",", "").replace("+", "").strip()
    if not cleaned or cleaned.upper() in ("-", "--", "N/A", "NA"):
        return 0
    return int(float(cleaned))


def _parse_pct(text: str) -> float:
    cleaned = text.replace("%", "").replace(",", "").strip()
    if not cleaned or cleaned.upper() in ("-", "--", "N/A", "NA"):
        return 0.0
    return float(cleaned)


def _fetch_djhtm_soup(path_and_query: str, timeout: int = 12) -> BeautifulSoup:
    last_err: Optional[Exception] = None
    for base in MIRROR_BASES:
        url = f"{base}{path_and_query}"
        try:
            resp = requests.get(url, headers=HEADERS, timeout=timeout)
            resp.raise_for_status()
            resp.encoding = "big5"
            return BeautifulSoup(resp.text, "html.parser")
        except Exception as exc:
            last_err = exc
            continue
    raise RuntimeError(f"無法連線至券商分點資料來源 ({path_and_query}): {last_err}")


def fetch_range_brokers(
    stock_id: str,
    start_date: str,
    end_date: str,
) -> BrokerRangeReport:
    """
    Fetch top-15 net-buy and top-15 net-sell broker branches for `stock_id`
    over [start_date, end_date] (YYYY-MM-DD).
    """
    path = f"/z/zc/zco/zco.djhtm?a={stock_id}&e={start_date}&f={end_date}"
    soup = _fetch_djhtm_soup(path)
    full_text = soup.get_text(" ", strip=True)

    # Extract stock name e.g. 台積電(2330)
    name_match = re.search(rf"([^\s(]+)\({re.escape(stock_id)}\)", full_text)
    stock_name = name_match.group(1) if name_match else stock_id

    # Extract last update date e.g. 最後更新日：2026/11/17
    date_match = re.search(r"最後更新日[：:]\s*(\d{4}/\d{2}/\d{2})", full_text)
    last_update_date = (
        date_match.group(1).replace("/", "-") if date_match else end_date
    )

    buy_top: list[BrokerSummaryRow] = []
    sell_top: list[BrokerSummaryRow] = []

    for tr in soup.select("table tr"):
        tds = tr.find_all("td")
        if len(tds) != 10:
            continue

        left_name = tds[0].get_text(strip=True)
        if left_name in ("買超券商", ""):
            # Header or empty left cell
            pass
        else:
            left_link = tds[0].find("a", href=True)
            buy_top.append(
                BrokerSummaryRow(
                    name=left_name,
                    buy_lots=_parse_int(tds[1].get_text(strip=True)),
                    sell_lots=_parse_int(tds[2].get_text(strip=True)),
                    net_lots=_parse_int(tds[3].get_text(strip=True)),
                    volume_pct=_parse_pct(tds[4].get_text(strip=True)),
                    zco0_href=left_link["href"] if left_link else None,
                )
            )

        right_name = tds[5].get_text(strip=True)
        if right_name not in ("賣超券商", ""):
            right_link = tds[5].find("a", href=True)
            sell_top.append(
                BrokerSummaryRow(
                    name=right_name,
                    buy_lots=_parse_int(tds[6].get_text(strip=True)),
                    sell_lots=_parse_int(tds[7].get_text(strip=True)),
                    net_lots=-abs(_parse_int(tds[8].get_text(strip=True))),
                    volume_pct=_parse_pct(tds[9].get_text(strip=True)),
                    zco0_href=right_link["href"] if right_link else None,
                )
            )

    return BrokerRangeReport(
        stock_id=stock_id,
        stock_name=stock_name,
        last_update_date=last_update_date,
        buy_top=buy_top,
        sell_top=sell_top,
    )


def _format_dj_date(date_str: str) -> str:
    """Convert YYYY-MM-DD to YYYY-M-D expected by djhtm custom date query."""
    parts = date_str.split("-")
    if len(parts) == 3:
        return f"{int(parts[0])}-{int(parts[1])}-{int(parts[2])}"
    return date_str


def fetch_branch_daily_history(
    zco0_href: str,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> list[BranchDailyRecord]:
    """
    Fetch daily buy/sell history for a single broker branch on a stock
    using the `zco0_href` link from `zco.djhtm`.
    Returns records sorted chronologically (oldest -> newest).
    """
    if not zco0_href:
        return []
    path = zco0_href if zco0_href.startswith("/") else f"/{zco0_href}"
    if start_date and end_date:
        sep = "&" if "?" in path else "?"
        path = (
            f"{path}{sep}C=1"
            f"&D={_format_dj_date(start_date)}"
            f"&E={_format_dj_date(end_date)}&ver=V3"
        )
    soup = _fetch_djhtm_soup(path)

    records: list[BranchDailyRecord] = []
    date_pattern = re.compile(r"^\d{4}/\d{2}/\d{2}$")

    for tr in soup.select("table tr"):
        tds = tr.find_all("td")
        if len(tds) != 5:
            continue
        date_str = tds[0].get_text(strip=True)
        if not date_pattern.match(date_str):
            continue
        records.append(
            BranchDailyRecord(
                date=date_str.replace("/", "-"),
                buy_lots=_parse_int(tds[1].get_text(strip=True)),
                sell_lots=_parse_int(tds[2].get_text(strip=True)),
                total_lots=_parse_int(tds[3].get_text(strip=True)),
                net_lots=_parse_int(tds[4].get_text(strip=True)),
            )
        )
    records.sort(key=lambda r: r.date)
    return records


def _is_na(text: str) -> bool:
    cleaned = text.strip().upper()
    return not cleaned or cleaned in ("-", "--", "N/A", "NA")


def _fetch_latest_margin_from_zcn(stock_id: str) -> tuple[str, int]:
    """Fetch latest published margin change date & value from zcn_{stock_id}.djhtm."""
    try:
        soup = _fetch_djhtm_soup(f"/z/zc/zcn/zcn_{stock_id}.djhtm")
        date_pat = re.compile(r"^\d{2,3}/\d{2}/\d{2}$")
        for tr in soup.select("table tr"):
            tds = [td.get_text(strip=True) for td in tr.find_all("td")]
            if len(tds) >= 6 and date_pat.match(tds[0]):
                return tds[0], _parse_int(tds[5])
    except Exception:
        pass
    return "", 0


def fetch_chip_flow_summary(stock_id: str) -> Optional[ChipFlowSummary]:
    """Fetch institutional & margin balance summary from zcj_{stock_id}.djhtm."""
    try:
        soup = _fetch_djhtm_soup(f"/z/zc/zcj/zcj_{stock_id}.djhtm")
    except Exception:
        return None

    full_txt = soup.get_text(" ", strip=True)
    dm = re.search(r"日期[：:]\s*(\d{1,2}/\d{1,2})", full_txt)
    summary = ChipFlowSummary(data_date=dm.group(1) if dm else "")
    found = False
    for tr in soup.select("table tr"):
        tds = [td.get_text(strip=True) for td in tr.find_all("td")]
        if len(tds) < 4:
            continue
        label = tds[0]
        if label == "外資持股":
            summary.foreign_lots = _parse_int(tds[1])
            summary.foreign_chg = _parse_int(tds[2])
            found = True
        elif label == "投信持股":
            summary.trust_lots = _parse_int(tds[1])
            summary.trust_chg = _parse_int(tds[2])
            found = True
        elif label == "自營商持股":
            summary.dealer_lots = _parse_int(tds[1])
            summary.dealer_chg = _parse_int(tds[2])
            found = True
        elif label == "融資餘額":
            summary.margin_balance = _parse_int(tds[1])
            summary.margin_available = not _is_na(tds[2])
            summary.margin_chg = _parse_int(tds[2]) if summary.margin_available else 0
            summary.margin_pct_capital = _parse_pct(tds[3])
            found = True
        elif label == "融券餘額":
            summary.short_balance = _parse_int(tds[1])
            summary.short_available = not _is_na(tds[2])
            summary.short_chg = _parse_int(tds[2]) if summary.short_available else 0
            found = True

    if found and not summary.margin_available:
        m_date, m_chg = _fetch_latest_margin_from_zcn(stock_id)
        summary.margin_latest_date = m_date
        summary.margin_latest_chg = m_chg

    return summary if found else None


def fetch_weekly_big_holders(stock_id: str) -> Optional[BigHolderWeekly]:
    """Fetch weekly TDCC big-holder & chip concentration trend from HiStock."""
    try:
        resp = requests.get(
            f"https://histock.tw/stock/large.aspx?no={stock_id}",
            headers=HEADERS,
            timeout=10,
        )
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        rows: list[tuple[str, float, float]] = []
        date_pat = re.compile(r"^\d{4}/\d{2}/\d{2}$")
        for tr in soup.select("table tr"):
            tds = [td.get_text(strip=True) for td in tr.find_all("td")]
            if len(tds) >= 5 and date_pat.match(tds[0]):
                conc = _parse_pct(tds[1])
                big = _parse_pct(tds[3])
                rows.append((tds[0], conc, big))
                if len(rows) >= 2:
                    break
        if not rows:
            return None
        latest_date, conc_now, big_now = rows[0]
        if len(rows) >= 2:
            _, conc_prev, big_prev = rows[1]
        else:
            conc_prev, big_prev = conc_now, big_now
        return BigHolderWeekly(
            latest_date=latest_date,
            big_holder_pct=round(big_now, 2),
            prev_big_holder_pct=round(big_prev, 2),
            big_holder_diff=round(big_now - big_prev, 2),
            concentration_pct=round(conc_now, 2),
            concentration_diff=round(conc_now - conc_prev, 2),
        )
    except Exception:
        return None
