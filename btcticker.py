#!/usr/bin/python3
"""
main.py — Unified Crypto + Stock Ticker for ePaper
Uses CoinGecko for crypto and Stooq for .US stock tickers.
"""

from babel.numbers import decimal, format_currency
from babel import Locale
import argparse
import textwrap
import socket
import yaml
import matplotlib.pyplot as plt
from PIL import Image, ImageOps, ImageDraw, ImageFont
import currency
import os
import sys
import logging
import RPi.GPIO as GPIO
from waveshare_epd import epd2in7
import time
import requests
import json
import matplotlib as mpl
import csv
from io import StringIO
mpl.use('Agg')

dirname = os.path.dirname(__file__)
picdir = os.path.join(os.path.dirname(os.path.realpath(__file__)), 'images')
fontdir = os.path.join(os.path.dirname(os.path.realpath(__file__)), 'fonts/googlefonts')
configfile = os.path.join(os.path.dirname(os.path.realpath(__file__)), 'config.yaml')
font_date = ImageFont.truetype(os.path.join(fontdir, 'PixelSplitter-Bold.ttf'), 11)
headers = {'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko)'}
button_pressed = 0


def internet(hostname="google.com"):
    try:
        host = socket.gethostbyname(hostname)
        s = socket.create_connection((host, 80), 2)
        s.close()
        return True
    except:
        time.sleep(1)
        return False


def human_format(num):
    num = float('{:.3g}'.format(num))
    magnitude = 0
    while abs(num) >= 1000:
        magnitude += 1
        num /= 1000.0
    return '{}{}'.format('{:f}'.format(num).rstrip('0').rstrip('.'),
                         ['', 'K', 'M', 'B', 'T'][magnitude])


def _place_text(img, text, x_offset=0, y_offset=0, fontsize=40, fontstring="Forum-Regular", fill=0):
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype(os.path.join(fontdir, fontstring + '.ttf'), fontsize)
    except OSError:
        font = ImageFont.truetype('/usr/share/fonts/TTF/DejaVuSans.ttf', fontsize)
    iw, ih = img.size
    tw = font.getbbox(text)[2]
    th = font.getbbox(text)[3]
    draw_x = (iw - tw) // 2 + x_offset
    draw_y = (ih - th) // 2 + y_offset
    draw.text((draw_x, draw_y), text, font=font, fill=fill)


def writewrappedlines(img, text, fontsize=16, y_text=20, height=15, width=25, fontstring="Roboto-Light"):
    lines = textwrap.wrap(text, width)
    for line in lines:
        _place_text(img, line, 0, y_text, fontsize, fontstring)
        y_text += height
    return img


def getgecko(url):
    try:
        j = requests.get(url, headers=headers).json()
        return j, False
    except requests.exceptions.RequestException:
        return {}, True


# --- Stooq for .US tickers ---
def getStockPrice(symbol, other):
    base_symbol = symbol.upper().replace(".US", "")
    url_quote = f"https://stooq.com/q/l/?s={base_symbol}.US&f=sd2t2ohlcvn&h&e=csv"
    url_hist = f"https://stooq.com/q/d/l/?s={base_symbol}.US&i=d"

    try:
        r = requests.get(url_quote, headers=headers, timeout=10)
        r.raise_for_status()
        row = list(csv.DictReader(StringIO(r.text)))[0]
        price = float(row.get("Close", "0") or 0)
        open_price = float(row.get("Open", "0") or 0)
        change_pct = ((price - open_price) / open_price) * 100.0 if open_price else 0.0
        volume = row.get("Volume", "0")
        other["volume"] = float(volume.replace(",", "")) if volume not in ("N/D", "-", "") else 0.0
        other["market_cap_rank"] = 0
        other["ATH"] = False

        h = requests.get(url_hist, headers=headers, timeout=10)
        timeseries = []
        rows = list(csv.DictReader(StringIO(h.text)))
        for r_ in rows[-14:]:
            val = r_.get("Close", "0")
            timeseries.append(float(val.replace(",", "")) if val not in ("N/D", "-", "") else 0.0)

        if not timeseries:
            timeseries = [price]

        logging.info(f"Stooq: {base_symbol} = ${price:.2f} ({change_pct:+.2f}%)")
        return timeseries, change_pct, other

    except Exception as e:
        logging.warning(f"Stooq lookup failed for {symbol}: {e}")
        return [0.0], 0.0, other


def getData(config, other):
    whichcoin, fiat = configtocoinandfiat(config)

    if whichcoin.endswith(".US"):
        entity = whichcoin.replace(".US", "")
        pricestack, change_pct, other = getStockPrice(entity, other)
        if pricestack == [0.0]:
            msg = f"{entity.upper()} price unavailable.\nSorry, I’m in the shower.\nTry again later!"
            logging.warning(msg)
            image = beanaproblem(msg)
            display_image(image)
            time.sleep(10)
            return [0.0], other
        return pricestack, other

    # --- normal CoinGecko ---
    sleep_time = 10
    num_retries = 5
    days_ago = int(config['ticker']['sparklinedays'])
    endtime = int(time.time())
    starttime = endtime - 60 * 60 * 24 * days_ago
    fiathistory = fiat if fiat.lower() != 'usdt' else 'usd'

    geckourlhistorical = (
        f"https://api.coingecko.com/api/v3/coins/{whichcoin}/market_chart/range?"
        f"vs_currency={fiathistory}&from={starttime}&to={endtime}"
    )

    timeseriesstack = []
    for _ in range(num_retries):
        raw, fail = getgecko(geckourlhistorical)
        if not fail:
            for arr in raw.get("prices", []):
                timeseriesstack.append(float(arr[1]))
            time.sleep(1)

        geckourl = f"https://api.coingecko.com/api/v3/coins/markets?vs_currency={fiathistory}&ids={whichcoin}"
        live, fail = getgecko(geckourl)
        if not fail and isinstance(live, list) and live:
            liveprice = live[0]
            pricenow = float(liveprice.get("current_price", 0))
            alltimehigh = float(liveprice.get("ath", 0))
            other["market_cap_rank"] = int(liveprice.get("market_cap_rank", 0) or 0)
            other["volume"] = float(liveprice.get("total_volume", 0))
            other["ATH"] = pricenow > alltimehigh
            timeseriesstack.append(pricenow)
        if fail:
            time.sleep(sleep_time)
            sleep_time *= 2
        else:
            break

    return timeseriesstack, other


def makeSpark(pricestack):
    if not pricestack or all(p == 0 for p in pricestack):
        pricestack = [0.0, 0.0]
    themean = sum(pricestack) / float(len(pricestack))
    x = [xx - themean for xx in pricestack]
    fig, ax = plt.subplots(1, 1, figsize=(10, 3))
    plt.plot(x, color='k', linewidth=6)
    plt.plot(len(x) - 1, x[-1], color='r', marker='o')
    for k, v in ax.spines.items():
        v.set_visible(False)
    ax.set_xticks([]); ax.set_yticks([])
    ax.axhline(c='k', linewidth=4, linestyle=(0, (5, 2, 1, 2)))
    out = os.path.join(picdir, 'spark.png')
    plt.savefig(out, dpi=17)
    img = Image.open(out)
    img.save(os.path.join(picdir, 'spark.bmp'))
    plt.close(fig)
    img.close()


def custom_format_currency(value, currency, locale):
    value = decimal.Decimal(value)
    locale = Locale.parse(locale)
    pattern = locale.currency_formats['standard']
    force_frac = ((0, 0) if value == int(value) else None)
    return pattern.apply(value, locale, currency=currency, force_frac=force_frac)


def updateDisplay(config, pricestack, other):
    whichcoin, fiat = configtocoinandfiat(config)
    days_ago = int(config['ticker']['sparklinedays'])
    pricenow = pricestack[-1] if pricestack else 0
    sparkbitmap = Image.open(os.path.join(picdir, 'spark.bmp'))
    ATHbitmap = Image.open(os.path.join(picdir, 'ATH.bmp'))

    tokenfile = os.path.join(picdir, f'currency/{whichcoin}.bmp')
    tokenimage = Image.open(tokenfile).convert("RGBA") if os.path.isfile(tokenfile) \
        else Image.open(os.path.join(picdir, 'currency/bitcoinINV.bmp')).convert("RGBA")

    # Safe percent change calc
    try:
        if pricestack and len(pricestack) > 1 and pricestack[0] != 0:
            pricechangeraw = round((pricestack[-1] - pricestack[0]) / pricestack[0] * 100, 2)
        else:
            pricechangeraw = 0.0
    except Exception:
        pricechangeraw = 0.0
    pricechange = f"{pricechangeraw:+.2f}%"

    timestamp = str(time.strftime("%-I:%M %p, %d %b %Y"))
    localetag = config['display'].get('locale', 'en_US')
    fiatupper = 'USD' if fiat.upper() == 'USDT' else fiat.upper()

    if whichcoin.endswith(".US"):
        display_symbol = whichcoin.replace(".US", "").upper()
        pricestring = f"{display_symbol}: ${human_format(pricenow)}"
    elif pricenow > 10000:
        pricestring = custom_format_currency(int(pricenow), fiatupper, localetag)
    elif pricenow > 0.01:
        pricestring = format_currency(pricenow, fiatupper, locale=localetag, decimal_quantization=False)
    else:
        pricestring = format_currency(pricenow, fiatupper, locale=localetag, decimal_quantization=False)

    fontreduce = 15 if len(pricestring) > 9 else 0

    image = Image.new('L', (264, 176), 255)
    draw = ImageDraw.Draw(image)
    if other.get('ATH', False):
        image.paste(ATHbitmap, (205, 85))
    draw.text((110, 90), f"{days_ago} day : {pricechange}", font=font_date, fill=0)

    if not whichcoin.endswith(".US"):
        vol_str = human_format(other.get("volume", 0))
        draw.text((110, 105), f"24h vol : {vol_str}", font=font_date, fill=0)

    writewrappedlines(image, pricestring, 50 - fontreduce, 50, 8, 15, "IBMPlexSans-Medium")
    image.paste(sparkbitmap, (80, 40))
    image.paste(tokenimage, (0, 10))

    draw.text((95, 15), timestamp, font=font_date, fill=0)
    if config['display'].get('inverted', False):
        image = ImageOps.invert(image)
    return image


# --- Helper + GPIO ---
def currencystringtolist(currstring):
    return [x.strip() for x in currstring.split(",")]


def configtocoinandfiat(config):
    c = currencystringtolist(config['ticker']['currency'])
    f = currencystringtolist(config['ticker']['fiatcurrency'])
    return c[0], f[0]


def fullupdate(config, lastcoinfetch):
    other = {}
    try:
        pricestack, other = getData(config, other)
        makeSpark(pricestack)
        img = updateDisplay(config, pricestack, other)
        display_image(img)
        time.sleep(0.2)
        return time.time()
    except Exception as e:
        tb = e.__traceback__
        lineno = tb.tb_lineno if tb else '?'
        logging.error(f"Update error: {e} (line {lineno})")
        img = beanaproblem(f"{e} @ line {lineno}")
        display_image(img)
        time.sleep(20)
        return lastcoinfetch


def beanaproblem(msg):
    thebean = Image.open(os.path.join(picdir, 'thebean.bmp'))
    image = Image.new('L', (264, 176), 255)
    draw = ImageDraw.Draw(image)
    image.paste(thebean, (60, 45))
    draw.text((95, 15), time.strftime("%-H:%M %p, %-d %b %Y"), font=font_date, fill=0)
    writewrappedlines(image, "Issue: " + msg)
    return image


def display_image(img):
    epd = epd2in7.EPD()
    epd.Init_4Gray()
    epd.display_4Gray(epd.getbuffer_4Gray(img))
    epd.sleep()
    keys = initkeys()
    removekeyevent(keys)
    addkeyevent(keys)


def initkeys():
    keys = [5, 6, 13, 19]
    GPIO.setmode(GPIO.BCM)
    for k in keys:
        GPIO.setup(k, GPIO.IN, pull_up_down=GPIO.PUD_UP)
    return keys


def addkeyevent(keys):
    for k in keys:
        GPIO.add_event_detect(k, GPIO.FALLING, callback=keypress, bouncetime=500)


def removekeyevent(keys):
    for k in keys:
        GPIO.remove_event_detect(k)


def keypress(channel):
    global button_pressed
    if button_pressed != 0:
        return
    with open(configfile) as f:
        config = yaml.load(f, Loader=yaml.FullLoader)
    button_pressed = 1
    if channel == 5:
        c = currencystringtolist(config['ticker']['currency'])
        config['ticker']['currency'] = ",".join(c[1:] + c[:1])
    elif channel == 6:
        config['display']['orientation'] = (config['display']['orientation'] + 90) % 360
    elif channel == 13:
        config['display']['inverted'] = not config['display']['inverted']
    elif channel == 19:
        f = currencystringtolist(config['ticker']['fiatcurrency'])
        config['ticker']['fiatcurrency'] = ",".join(f[1:] + f[:1])
    fullupdate(config, time.time())
    configwrite(config)
    button_pressed = 0


def configwrite(config):
    with open(configfile, 'w') as f:
        yaml.dump(config, f)


def main():
    GPIO.setmode(GPIO.BCM)
    parser = argparse.ArgumentParser()
    parser.add_argument("--log", default='info', help='Log level (default: info)')
    args = parser.parse_args()
    logging.basicConfig(level=getattr(logging, args.log.upper(), logging.INFO))

    with open(configfile) as f:
        config = yaml.load(f, Loader=yaml.FullLoader)

    thekeys = initkeys()
    addkeyevent(thekeys)
    lastcoinfetch = time.time()
    datapulled = False
    updatefrequency = max(float(config['ticker'].get('updatefrequency', 60)), 60)

    while not internet():
        logging.info("Waiting for internet...")
        time.sleep(1)

    while True:
        if (time.time() - lastcoinfetch > updatefrequency) or not datapulled:
            if config['display'].get('cycle', False) and datapulled:
                crypto_list = currencystringtolist(config['ticker']['currency'])
                if len(crypto_list) > 1:
                    crypto_list = crypto_list[1:] + crypto_list[:1]
                    config['ticker']['currency'] = ",".join(crypto_list)

                if config['display'].get('cyclefiat', False):
                    fiat_list = currencystringtolist(config['ticker']['fiatcurrency'])
                    if len(fiat_list) > 1:
                        fiat_list = fiat_list[1:] + fiat_list[:1]
                        config['ticker']['fiatcurrency'] = ",".join(fiat_list)

            lastcoinfetch = fullupdate(config, lastcoinfetch)
            datapulled = True

        time.sleep(0.01)


if __name__ == "__main__":
    main()
