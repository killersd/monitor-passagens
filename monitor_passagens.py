#!/usr/bin/env python3
"""Monitor de passagens aereas (Google Flights) com alerta no Telegram.

Padrao: Aracaju (AJU) -> Guarulhos (GRU), ida e volta, 1 adulto,
ida 24/04/2027, volta 26/04/2027, alerta abaixo de R$ 1000.

Usa somente a biblioteca padrao do Python.
"""

from __future__ import annotations

import argparse
import base64
import gzip
import html as html_mod
import json
import logging
import os
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
STATE_PATH = BASE_DIR / "estado.json"
LOG_PATH = BASE_DIR / "monitor.log"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

log = logging.getLogger("passagens")


# ---------------------------------------------------------------- protobuf tfs
# O parametro "tfs" do Google Flights e um protobuf serializado em base64url.


def _varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _tag(field: int, wire: int) -> bytes:
    return _varint((field << 3) | wire)


def _field_str(field: int, value: str) -> bytes:
    raw = value.encode("utf-8")
    return _tag(field, 2) + _varint(len(raw)) + raw


def _field_msg(field: int, value: bytes) -> bytes:
    return _tag(field, 2) + _varint(len(value)) + value


def _field_int(field: int, value: int) -> bytes:
    return _tag(field, 0) + _varint(value)


def _leg(date: str, origin: str, destination: str) -> bytes:
    return (
        _field_str(2, date)
        + _field_msg(13, _field_str(2, origin))
        + _field_msg(14, _field_str(2, destination))
    )


SEAT_CLASSES = {"economica": 1, "premium": 2, "executiva": 3, "primeira": 4}


def build_tfs(legs: list[bytes], adults: int, seat: int, round_trip: bool) -> str:
    payload = b""
    for leg in legs:
        payload += _field_msg(3, leg)
    payload += _field_int(8, 1) * adults                # 1 = adulto, repetido por passageiro
    payload += _field_int(9, seat)                      # classe da cabine
    payload += _field_int(19, 1 if round_trip else 2)   # 1 = ida e volta, 2 = so ida
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def build_url(cfg: dict) -> str:
    seat = SEAT_CLASSES.get(str(cfg.get("classe", "economica")).lower(), 1)
    legs = [_leg(cfg["data_ida"], cfg["origem"], cfg["destino"])]
    round_trip = bool(cfg.get("data_volta"))
    if round_trip:
        legs.append(_leg(cfg["data_volta"], cfg["destino"], cfg["origem"]))
    tfs = build_tfs(legs, int(cfg.get("adultos", 1)), seat, round_trip)
    query = urllib.parse.urlencode(
        {
            "tfs": tfs,
            "hl": "pt-BR",
            "gl": "BR",
            "curr": cfg.get("moeda", "BRL"),
            "tfu": "EgQIABABIgA",  # pede a lista completa de resultados
        }
    )
    return "https://www.google.com/travel/flights?" + query


# ------------------------------------------------------------------- coleta web


def fetch(url: str, timeout: int = 60) -> str:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
            "Accept-Encoding": "gzip, deflate",
            "Cache-Control": "no-cache",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        encoding = (resp.headers.get("Content-Encoding") or "").lower()
    if encoding == "gzip":
        raw = gzip.decompress(raw)
    elif encoding == "deflate":
        raw = zlib.decompress(raw, -zlib.MAX_WBITS)
    return raw.decode("utf-8", "ignore")


# ---------------------------------------------------------------------- parsing


@dataclass
class Voo:
    preco: int
    companhia: str
    partida: str
    chegada: str
    duracao: str
    paradas: str

    def chave(self) -> str:
        return "{}|{}|{}|{}".format(self.companhia, self.partida, self.chegada, self.paradas)

    def resumo(self) -> str:
        preco = "R$ {:,}".format(self.preco).replace(",", ".")
        return "{} | {} | {} -> {} | {} | {}".format(
            preco, self.companhia, self.partida, self.chegada, self.duracao, self.paradas
        )


_TAGS = re.compile(r"<[^>]+>")
_SPACES = re.compile(r"[\s ]+")
_PRICE = re.compile(r"R\$[\s ]*([\d\.]+)")
_TIME = re.compile(r"^\d{1,2}:\d{2}$")
_DURATION = re.compile(r"\d+\s*h(?:\s*\d+\s*min)?|\d+\s*min")
_STOPS = re.compile(r"Sem escalas|\d+\s+parada(?:s)?(?: em [^|]+?)?(?= \|)")
_IGNORAR = re.compile(r"^(Partida|Chegada|Selecionar|Operado|Evita|R\$|\d)", re.IGNORECASE)


def _text_parts(fragment: str) -> list[str]:
    plain = html_mod.unescape(_TAGS.sub("|", fragment))
    parts = []
    for chunk in plain.split("|"):
        chunk = _SPACES.sub(" ", chunk).strip()
        if chunk:
            parts.append(chunk)
    return parts


def parse_voos(page: str) -> list[Voo]:
    voos: list[Voo] = []
    blocks = re.split(r'(?=<li class="pIav2d")', page)[1:]
    for block in blocks:
        parts = _text_parts(block[:40000])
        joined = " | ".join(parts)

        price_match = _PRICE.search(joined)
        if not price_match:
            continue
        try:
            preco = int(price_match.group(1).replace(".", ""))
        except ValueError:
            continue
        if preco <= 0:
            continue

        horarios = [p for p in parts if _TIME.match(p)]
        if len(horarios) < 2:
            continue

        stops_match = _STOPS.search(joined)
        dur_match = _DURATION.search(joined)

        companhia = "?"
        for part in parts:
            if _TIME.match(part) or _DURATION.fullmatch(part) or _IGNORAR.match(part):
                continue
            if len(part) < 2 or len(part) > 40 or "em " in part or part in {"-", "–"}:
                continue
            companhia = part
            break

        voos.append(
            Voo(
                preco=preco,
                companhia=companhia,
                partida=horarios[0],
                chegada=horarios[1],
                duracao=dur_match.group(0) if dur_match else "?",
                paradas=stops_match.group(0) if stops_match else "?",
            )
        )

    unicos: dict[str, Voo] = {}
    for voo in voos:
        chave = voo.chave()
        if chave not in unicos or voo.preco < unicos[chave].preco:
            unicos[chave] = voo
    return sorted(unicos.values(), key=lambda v: v.preco)


# --------------------------------------------------------------------- telegram


def telegram_send(token: str, chat_id: str, texto: str) -> bool:
    if not token or not chat_id:
        log.error("Telegram nao configurado: preencha telegram_bot_token e telegram_chat_id.")
        return False
    url = "https://api.telegram.org/bot{}/sendMessage".format(token)
    payload = urllib.parse.urlencode(
        {
            "chat_id": chat_id,
            "text": texto,
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        }
    ).encode()
    req = urllib.request.Request(
        url, data=payload, headers={"Content-Type": "application/x-www-form-urlencoded"}
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read().decode("utf-8", "ignore"))
        if body.get("ok"):
            return True
        log.error("Telegram recusou o envio: %s", body)
    except urllib.error.HTTPError as exc:
        detalhe = exc.read().decode("utf-8", "ignore")[:300]
        log.error("Telegram HTTP %s: %s", exc.code, detalhe)
    except Exception as exc:  # noqa: BLE001
        log.error("Falha ao falar com o Telegram: %s", exc)
    return False


def _br_data(iso: str) -> str:
    return datetime.strptime(iso, "%Y-%m-%d").strftime("%d/%m/%Y")


def monta_mensagem(cfg: dict, voos: list[Voo], url: str, limite: int) -> str:
    volta = cfg.get("data_volta")
    linhas = [
        "<b>Passagem abaixo de R$ {}!</b>".format(limite),
        "",
        "Trecho: {} -> {}{}".format(
            cfg["origem"], cfg["destino"], " (ida e volta)" if volta else " (so ida)"
        ),
        "Ida: {}{}".format(
            _br_data(cfg["data_ida"]), " | Volta: " + _br_data(volta) if volta else ""
        ),
        "Passageiros: {} adulto(s) | Classe: {}".format(
            cfg.get("adultos", 1), cfg.get("classe", "economica")
        ),
        "",
        "<b>Melhores ofertas</b>",
    ]
    for voo in voos[:5]:
        linhas.append("- " + html_mod.escape(voo.resumo()))
    linhas += [
        "",
        '<a href="{}">Abrir no Google Flights</a>'.format(html_mod.escape(url, quote=True)),
        "<i>Verificado em {}</i>".format(datetime.now().strftime("%d/%m/%Y %H:%M:%S")),
    ]
    return "\n".join(linhas)


# ------------------------------------------------------------- config e estado


DEFAULT_CONFIG = {
    "origem": "AJU",
    "destino": "GRU",
    "data_ida": "2027-04-24",
    "data_volta": "2027-04-26",
    "adultos": 1,
    "classe": "economica",
    "moeda": "BRL",
    "preco_maximo": 1000,
    "intervalo_minutos": 60,
    "reenviar_apos_horas": 12,
    "telegram_bot_token": "",
    "telegram_chat_id": "",
}


def carrega_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
        except json.JSONDecodeError as exc:
            raise SystemExit("config.json invalido: {}".format(exc))
    else:
        CONFIG_PATH.write_text(
            json.dumps(DEFAULT_CONFIG, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        log.info("config.json criado com os valores padrao em %s", CONFIG_PATH)
    cfg["telegram_bot_token"] = os.getenv("TELEGRAM_BOT_TOKEN") or cfg["telegram_bot_token"]
    cfg["telegram_chat_id"] = os.getenv("TELEGRAM_CHAT_ID") or cfg["telegram_chat_id"]
    return cfg


def carrega_estado() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {}


def salva_estado(estado: dict) -> None:
    STATE_PATH.write_text(json.dumps(estado, indent=2, ensure_ascii=False), encoding="utf-8")


def deve_notificar(estado: dict, preco: int, reenviar_apos_horas: float) -> bool:
    ultimo = estado.get("ultimo_preco_alertado")
    if ultimo is None or preco < ultimo:
        return True
    quando = estado.get("ultimo_alerta_em", 0)
    return (time.time() - quando) >= reenviar_apos_horas * 3600


# ------------------------------------------------------------------ ciclo main


def verifica(cfg: dict, estado: dict, notificar: bool = True) -> list[Voo]:
    url = build_url(cfg)
    limite = int(cfg["preco_maximo"])
    log.info("Consultando %s -> %s", cfg["origem"], cfg["destino"])
    log.debug("URL: %s", url)

    page = fetch(url)
    voos = parse_voos(page)
    estado["ultima_verificacao"] = datetime.now().isoformat(timespec="seconds")

    if not voos:
        log.warning(
            "Nenhum preco extraido (pagina com %d caracteres). "
            "O Google pode ter mudado o layout ou bloqueado a consulta.",
            len(page),
        )
        salva_estado(estado)
        return []

    log.info("%d itinerarios lidos. Mais barato: %s", len(voos), voos[0].resumo())
    estado["menor_preco_visto"] = min(voos[0].preco, estado.get("menor_preco_visto", voos[0].preco))
    estado["melhores"] = [asdict(v) for v in voos[:5]]

    baratos = [v for v in voos if v.preco < limite]
    if not baratos:
        log.info("Nada abaixo de R$ %d por enquanto.", limite)
        salva_estado(estado)
        return voos

    menor = baratos[0].preco
    log.info("ACHOU: %d opcao(oes) abaixo de R$ %d (menor: R$ %d)", len(baratos), limite, menor)
    if notificar:
        if deve_notificar(estado, menor, float(cfg.get("reenviar_apos_horas", 12))):
            texto = monta_mensagem(cfg, baratos, url, limite)
            if telegram_send(cfg["telegram_bot_token"], cfg["telegram_chat_id"], texto):
                log.info("Alerta enviado no Telegram.")
                estado["ultimo_preco_alertado"] = menor
                estado["ultimo_alerta_em"] = time.time()
        else:
            log.info("Alerta desse patamar de preco ja enviado; aguardando janela de reenvio.")

    salva_estado(estado)
    return voos


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Monitor de passagens aereas com alerta no Telegram"
    )
    parser.add_argument("--once", action="store_true", help="faz uma unica consulta e sai")
    parser.add_argument("--intervalo", type=float, help="minutos entre consultas (modo continuo)")
    parser.add_argument("--preco-maximo", type=int, help="preco limite em reais para alertar")
    parser.add_argument("--origem", help="codigo IATA de origem (ex.: AJU)")
    parser.add_argument("--destino", help="codigo IATA de destino (ex.: GRU)")
    parser.add_argument("--data-ida", help="data de ida AAAA-MM-DD")
    parser.add_argument("--data-volta", help="data de volta AAAA-MM-DD")
    parser.add_argument("--adultos", type=int, help="quantidade de adultos")
    parser.add_argument(
        "--sem-notificar", action="store_true", help="apenas consulta, sem enviar Telegram"
    )
    parser.add_argument(
        "--testar-telegram", action="store_true", help="envia mensagem de teste e sai"
    )
    parser.add_argument("--verbose", action="store_true", help="log mais detalhado")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%d/%m/%Y %H:%M:%S",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(LOG_PATH, encoding="utf-8"),
        ],
    )

    cfg = carrega_config()
    for chave, valor in (
        ("origem", args.origem),
        ("destino", args.destino),
        ("data_ida", args.data_ida),
        ("data_volta", args.data_volta),
        ("adultos", args.adultos),
        ("preco_maximo", args.preco_maximo),
        ("intervalo_minutos", args.intervalo),
    ):
        if valor is not None:
            cfg[chave] = valor

    if args.testar_telegram:
        ok = telegram_send(
            cfg["telegram_bot_token"],
            cfg["telegram_chat_id"],
            "Monitor de passagens: teste de conexao OK.",
        )
        print("Mensagem enviada." if ok else "Falhou. Confira o token e o chat_id.")
        return 0 if ok else 1

    estado = carrega_estado()
    notificar = not args.sem_notificar

    if args.once:
        try:
            verifica(cfg, estado, notificar)
        except Exception as exc:  # noqa: BLE001
            log.error("Erro na consulta: %s", exc)
            return 1
        return 0

    intervalo = float(cfg.get("intervalo_minutos", 60))
    log.info("Modo continuo: uma consulta a cada %.0f minuto(s). Ctrl+C para parar.", intervalo)
    falhas = 0
    while True:
        try:
            verifica(cfg, estado, notificar)
            falhas = 0
        except KeyboardInterrupt:
            log.info("Encerrado pelo usuario.")
            return 0
        except Exception as exc:  # noqa: BLE001
            falhas += 1
            log.error("Erro na consulta (%d seguidas): %s", falhas, exc)
        espera = intervalo * 60 * (1 + min(falhas, 4)) + random.uniform(0, 45)
        try:
            time.sleep(espera)
        except KeyboardInterrupt:
            log.info("Encerrado pelo usuario.")
            return 0


if __name__ == "__main__":
    raise SystemExit(main())
