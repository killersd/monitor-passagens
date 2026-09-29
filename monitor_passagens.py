#!/usr/bin/env python3
"""Monitor de passagens aereas (Google Flights) com alerta no Telegram.

Padrao: Aracaju (AJU) -> Guarulhos (GRU), ida e volta, 1 adulto,
ida 24/04/2027, volta 26/04/2027, alerta abaixo de R$ 1000.

Cada verificacao faz quatro consultas: a lista de ida e a lista de volta da
pesquisa de ida e volta (que traz o preco total) e uma pesquisa de so ida para
cada trecho (que traz o preco separado de cada um).

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
from dataclasses import dataclass, asdict, field
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


def _tag(field_num: int, wire: int) -> bytes:
    return _varint((field_num << 3) | wire)


def _field_str(field_num: int, value: str) -> bytes:
    raw = value.encode("utf-8")
    return _tag(field_num, 2) + _varint(len(raw)) + raw


def _field_msg(field_num: int, value: bytes) -> bytes:
    return _tag(field_num, 2) + _varint(len(value)) + value


def _field_int(field_num: int, value: int) -> bytes:
    return _tag(field_num, 0) + _varint(value)


def _leg(date: str, origin: str, destination: str) -> bytes:
    return (
        _field_str(2, date)
        + _field_msg(13, _field_str(2, origin))
        + _field_msg(14, _field_str(2, destination))
    )


SEAT_CLASSES = {"economica": 1, "premium": 2, "executiva": 3, "primeira": 4}

# Os quatro tipos de consulta de um ciclo.
IDA_TOTAL = "ida_total"
VOLTA_TOTAL = "volta_total"
IDA_AVULSA = "ida_avulsa"
VOLTA_AVULSA = "volta_avulsa"

ROTULOS = {
    IDA_TOTAL: "ida (total ida e volta)",
    VOLTA_TOTAL: "volta (total ida e volta)",
    IDA_AVULSA: "ida (so ida)",
    VOLTA_AVULSA: "volta (so ida)",
}


def build_tfs(legs: list[bytes], adults: int, seat: int, round_trip: bool) -> str:
    payload = b""
    for leg in legs:
        payload += _field_msg(3, leg)
    payload += _field_int(8, 1) * adults                # 1 = adulto, repetido por passageiro
    payload += _field_int(9, seat)                      # classe da cabine
    payload += _field_int(19, 1 if round_trip else 2)   # 1 = ida e volta, 2 = so ida
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def build_url(cfg: dict, modo: str = IDA_TOTAL) -> str:
    """Monta a URL do Google Flights para um dos quatro tipos de consulta.

    Nas consultas de ida e volta o preco exibido e sempre o total da viagem; para
    ver as opcoes do trecho de volta basta enviar os trechos na ordem inversa. O
    preco de cada trecho isolado sai das consultas de so ida, porque o Google nao
    aceita indicar por URL que a ida ja foi escolhida.
    """
    seat = SEAT_CLASSES.get(str(cfg.get("classe", "economica")).lower(), 1)
    ida = _leg(cfg["data_ida"], cfg["origem"], cfg["destino"])
    tem_volta = bool(cfg.get("data_volta"))
    volta = _leg(cfg["data_volta"], cfg["destino"], cfg["origem"]) if tem_volta else None

    if modo == IDA_AVULSA:
        legs, round_trip = [ida], False
    elif modo == VOLTA_AVULSA:
        legs, round_trip = [volta], False
    elif modo == VOLTA_TOTAL and tem_volta:
        legs, round_trip = [volta, ida], True
    else:
        legs = [ida, volta] if tem_volta else [ida]
        round_trip = tem_volta

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


def moeda(valor: int | None) -> str:
    if valor is None:
        return "R$ ?"
    return "R$ {:,}".format(valor).replace(",", ".")


@dataclass
class Voo:
    companhia: str
    partida: str
    chegada: str
    duracao: str
    paradas: str
    preco_trecho: int | None = None   # preco deste trecho comprado como so ida
    preco_total: int | None = None    # menor total de ida e volta com este voo

    def chave(self) -> str:
        return "{}|{}|{}".format(self.partida, self.chegada, self.paradas)

    def preco_ordenacao(self) -> int:
        for valor in (self.preco_total, self.preco_trecho):
            if valor is not None:
                return valor
        return 10**9

    def resumo(self) -> str:
        return "{} -> {} | {} | {} | {} | trecho {} | total {}".format(
            self.partida,
            self.chegada,
            self.companhia,
            self.duracao,
            self.paradas,
            moeda(self.preco_trecho),
            moeda(self.preco_total),
        )

    def identificacao(self) -> str:
        """Companhia e horarios, para acompanhar o preco no resumo."""
        return "{} {} -> {} ({})".format(self.companhia, self.partida, self.chegada, self.paradas)


@dataclass
class Resultado:
    ida: list[Voo] = field(default_factory=list)
    volta: list[Voo] = field(default_factory=list)
    # voos mais baratos de cada trecho comprados como so ida
    voo_ida: Voo | None = None
    voo_volta: Voo | None = None
    # voos mais baratos pelo total de ida e volta comprado junto
    voo_total_ida: Voo | None = None
    voo_total_volta: Voo | None = None

    @property
    def menor_ida(self) -> int | None:
        return self.voo_ida.preco_trecho if self.voo_ida else None

    @property
    def menor_volta(self) -> int | None:
        return self.voo_volta.preco_trecho if self.voo_volta else None

    @property
    def menor_total(self) -> int | None:
        totais = [
            v.preco_total
            for v in (self.voo_total_ida, self.voo_total_volta)
            if v is not None and v.preco_total is not None
        ]
        return min(totais) if totais else None

    @property
    def soma_trechos(self) -> int | None:
        if self.menor_ida is None or self.menor_volta is None:
            return None
        return self.menor_ida + self.menor_volta

    def melhor_preco(self) -> int | None:
        candidatos = [p for p in (self.menor_total, self.soma_trechos) if p is not None]
        return min(candidatos) if candidatos else None


_TAGS = re.compile(r"<[^>]+>")
_SPACES = re.compile(r"[\s ]+")
_PRICE = re.compile(r"R\$[\s ]*([\d\.]+)")
_TIME = re.compile(r"^\d{1,2}:\d{2}$")
_DURATION = re.compile(r"\d+\s*h(?:\s*\d+\s*min)?|\d+\s*min")
_STOPS = re.compile(r"Sem escalas|\d+\s+parada(?:s)?(?: em [^|]+?)?(?= \|)")
_IGNORAR = re.compile(r"^(Partida|Chegada|Selecionar|Operado|Evita|Voo|R\$|\+|\d)", re.IGNORECASE)


def _text_parts(fragment: str) -> list[str]:
    plain = html_mod.unescape(_TAGS.sub("|", fragment))
    parts = []
    for chunk in plain.split("|"):
        chunk = _SPACES.sub(" ", chunk).strip()
        if chunk:
            parts.append(chunk)
    return parts


def parse_voos(page: str, campo_preco: str) -> list[Voo]:
    """Extrai os itinerarios da pagina, gravando o preco em campo_preco."""
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

        voo = Voo(
            companhia=companhia,
            partida=horarios[0],
            chegada=horarios[1],
            duracao=dur_match.group(0) if dur_match else "?",
            paradas=stops_match.group(0) if stops_match else "?",
        )
        setattr(voo, campo_preco, preco)
        voos.append(voo)

    unicos: dict[str, Voo] = {}
    for voo in voos:
        chave = voo.chave()
        anterior = unicos.get(chave)
        if anterior is None or voo.preco_ordenacao() < anterior.preco_ordenacao():
            unicos[chave] = voo
    return sorted(unicos.values(), key=lambda v: v.preco_ordenacao())


def combina(totais: list[Voo], avulsos: list[Voo]) -> list[Voo]:
    """Junta as duas visoes do mesmo trecho: total de ida e volta + preco avulso."""
    por_chave = {v.chave(): v for v in totais}
    for avulso in avulsos:
        existente = por_chave.get(avulso.chave())
        if existente is None:
            por_chave[avulso.chave()] = avulso
        elif existente.preco_trecho is None:
            existente.preco_trecho = avulso.preco_trecho
    return sorted(por_chave.values(), key=lambda v: v.preco_ordenacao())


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


def _linha_preco(rotulo: str, valor: int | None, *voos: Voo | None) -> str:
    """Linha do resumo: preco em destaque, seguido da companhia e do horario."""
    partes = []
    for voo in voos:
        if voo is None:
            continue
        if valor is not None and valor not in (voo.preco_trecho, voo.preco_total):
            continue
        partes.append(html_mod.escape(voo.identificacao()))
    linha = "{}: <b>{}</b>".format(rotulo, moeda(valor))
    if partes:
        linha += " - " + " + ".join(partes)
    return linha


def monta_mensagem(cfg: dict, res: Resultado, limite: int, quantos: int = 3) -> str:
    data_volta = cfg.get("data_volta")
    linhas = [
        "<b>Passagem abaixo de R$ {}!</b>".format(limite),
        "",
        "Trecho: {} -> {}{}".format(
            cfg["origem"], cfg["destino"], " (ida e volta)" if data_volta else " (so ida)"
        ),
        "Ida: {}{}".format(
            _br_data(cfg["data_ida"]), " | Volta: " + _br_data(data_volta) if data_volta else ""
        ),
        "Passageiros: {} adulto(s) | Classe: {}".format(
            cfg.get("adultos", 1), cfg.get("classe", "economica")
        ),
        "",
        "<b>PRECOS</b>",
        _linha_preco("Ida ({})".format(_br_data(cfg["data_ida"])), res.menor_ida, res.voo_ida),
    ]
    if data_volta:
        linhas += [
            _linha_preco(
                "Volta ({})".format(_br_data(data_volta)), res.menor_volta, res.voo_volta
            ),
            "Soma dos dois trechos: <b>{}</b>".format(moeda(res.soma_trechos)),
            _linha_preco(
                "Total ida e volta (comprando junto)",
                res.menor_total,
                res.voo_total_ida,
                res.voo_total_volta,
            ),
        ]

    linhas += [
        "",
        "<b>IDA {} - {} -> {}</b>".format(_br_data(cfg["data_ida"]), cfg["origem"], cfg["destino"]),
    ]
    for voo in res.ida[:quantos]:
        linhas.append("- " + html_mod.escape(voo.resumo()))
    if not res.ida:
        linhas.append("- sem opcoes lidas neste trecho")

    if data_volta:
        linhas += [
            "",
            "<b>VOLTA {} - {} -> {}</b>".format(
                _br_data(data_volta), cfg["destino"], cfg["origem"]
            ),
        ]
        for voo in res.volta[:quantos]:
            linhas.append("- " + html_mod.escape(voo.resumo()))
        if not res.volta:
            linhas.append("- sem opcoes lidas neste trecho")
        linhas += [
            "",
            "<i>trecho = preco do voo comprado como so ida. total = menor preco de "
            "ida e volta combinando esse voo com o outro trecho.</i>",
        ]

    url = html_mod.escape(build_url(cfg, IDA_TOTAL), quote=True)
    linhas += [
        "",
        '<a href="{}">Abrir no Google Flights</a>'.format(url),
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


def consulta(cfg: dict, modo: str) -> list[Voo]:
    campo = "preco_total" if modo in (IDA_TOTAL, VOLTA_TOTAL) else "preco_trecho"
    url = build_url(cfg, modo)
    log.debug("URL (%s): %s", ROTULOS[modo], url)
    page = fetch(url)
    voos = parse_voos(page, campo)
    if not voos:
        log.warning(
            "Nenhum preco extraido em %s (pagina com %d caracteres). "
            "O Google pode ter mudado o layout ou bloqueado a consulta.",
            ROTULOS[modo],
            len(page),
        )
    else:
        log.debug("%s: %d itinerarios", ROTULOS[modo], len(voos))
    return voos


def coleta(cfg: dict) -> Resultado:
    tem_volta = bool(cfg.get("data_volta"))
    modos = [IDA_TOTAL, IDA_AVULSA] if not tem_volta else [
        IDA_TOTAL,
        VOLTA_TOTAL,
        IDA_AVULSA,
        VOLTA_AVULSA,
    ]
    dados: dict[str, list[Voo]] = {}
    for indice, modo in enumerate(modos):
        if indice:
            time.sleep(random.uniform(1.5, 4.0))  # espaca as consultas
        dados[modo] = consulta(cfg, modo)

    res = Resultado()
    res.ida = combina(dados.get(IDA_TOTAL, []), dados.get(IDA_AVULSA, []))
    res.volta = combina(dados.get(VOLTA_TOTAL, []), dados.get(VOLTA_AVULSA, []))

    def mais_barato(voos: list[Voo], atributo: str) -> Voo | None:
        candidatos = [v for v in voos if getattr(v, atributo) is not None]
        return min(candidatos, key=lambda v: getattr(v, atributo)) if candidatos else None

    # o preco do trecho avulso vem da pesquisa de so ida; o total, da de ida e volta,
    # mas a lista combinada e usada para que a companhia venha junto do preco
    res.voo_ida = mais_barato(res.ida, "preco_trecho")
    res.voo_volta = mais_barato(res.volta, "preco_trecho")
    res.voo_total_ida = mais_barato(res.ida, "preco_total")
    res.voo_total_volta = mais_barato(res.volta, "preco_total")
    return res


def verifica(cfg: dict, estado: dict, notificar: bool = True) -> Resultado:
    limite = int(cfg["preco_maximo"])
    log.info("Consultando %s -> %s", cfg["origem"], cfg["destino"])

    res = coleta(cfg)
    estado["ultima_verificacao"] = datetime.now().isoformat(timespec="seconds")

    if not res.ida and not res.volta:
        salva_estado(estado)
        return res

    def com_voo(valor: int | None, voo: Voo | None) -> str:
        if voo is None:
            return moeda(valor)
        return "{} ({})".format(moeda(valor), voo.identificacao())

    log.info(
        "Ida: %s | Volta: %s | Soma: %s | Total ida e volta: %s",
        com_voo(res.menor_ida, res.voo_ida),
        com_voo(res.menor_volta, res.voo_volta),
        moeda(res.soma_trechos),
        moeda(res.menor_total),
    )

    melhor = res.melhor_preco()
    estado["menor_preco_ida"] = res.menor_ida
    estado["menor_preco_volta"] = res.menor_volta
    estado["menor_total"] = res.menor_total
    estado["companhia_ida"] = res.voo_ida.companhia if res.voo_ida else None
    estado["companhia_volta"] = res.voo_volta.companhia if res.voo_volta else None
    estado["melhores_ida"] = [asdict(v) for v in res.ida[:5]]
    estado["melhores_volta"] = [asdict(v) for v in res.volta[:5]]
    if melhor is not None:
        estado["menor_preco_visto"] = min(melhor, estado.get("menor_preco_visto", melhor))

    if melhor is None or melhor >= limite:
        log.info("Nada abaixo de R$ %d por enquanto (melhor: %s).", limite, moeda(melhor))
        salva_estado(estado)
        return res

    log.info("ACHOU: total de %s, abaixo de R$ %d", moeda(melhor), limite)
    if notificar:
        if deve_notificar(estado, melhor, float(cfg.get("reenviar_apos_horas", 12))):
            texto = monta_mensagem(cfg, res, limite)
            if telegram_send(cfg["telegram_bot_token"], cfg["telegram_chat_id"], texto):
                log.info("Alerta enviado no Telegram.")
                estado["ultimo_preco_alertado"] = melhor
                estado["ultimo_alerta_em"] = time.time()
        else:
            log.info("Alerta desse patamar de preco ja enviado; aguardando janela de reenvio.")

    salva_estado(estado)
    return res


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
    parser.add_argument(
        "--previa", action="store_true", help="manda no Telegram o alerta com os precos de agora"
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

    if args.previa:
        res = coleta(cfg)
        texto = monta_mensagem(cfg, res, int(cfg["preco_maximo"]))
        print(re.sub(r"<[^>]+>", "", texto))
        ok = telegram_send(cfg["telegram_bot_token"], cfg["telegram_chat_id"], texto)
        print("\nMensagem enviada." if ok else "\nNao foi enviada; confira as credenciais.")
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
