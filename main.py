#!/usr/bin/env python3
"""Descarga las portadas de los anuncios visibles en Vehículos de Marketplace."""

from __future__ import annotations

import csv
import hashlib
import io
import os
import re
import sys
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, urljoin, urlparse

from PIL import Image, ImageOps, UnidentifiedImageError
from playwright.sync_api import BrowserContext, Page, TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright


BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "marketplace_portadas"
SESSION_DIR = BASE_DIR / "facebook_session"
CSV_PATH = BASE_DIR / "marketplace_portadas.csv"
MODEL_PATH = BASE_DIR / "yolo11n.pt"
CLASSIFIER_MODEL_PATH = BASE_DIR / "yolo11n-cls.pt"
YOLO_CONFIG_DIR = BASE_DIR / ".yolo_config"
os.environ.setdefault("YOLO_CONFIG_DIR", str(YOLO_CONFIG_DIR))
VEHICLES_URL = "https://www.facebook.com/marketplace/category/vehicles/"
MOBILE_VEHICLES_URL = "https://m.facebook.com/marketplace/category/vehicles/"
LOGIN_URL = (
    "https://m.facebook.com/login/?next=" + quote(MOBILE_VEHICLES_URL, safe="")
)

HEADLESS = False
SCROLL_PAUSE_MS = 1_500
MAX_SCROLLS = 250
UNCHANGED_LIMIT = 7
LOGIN_WAIT_SECONDS = 15 * 60


@dataclass
class Listing:
    item_key: str
    title: str
    listing_url: str
    image_url: str
    image_width: int = 0
    search_text: str = ""


def clean_title(value: str | None) -> str:
    text = re.sub(r"\s+", " ", value or "").strip()
    # En la interfaz actual, el aria-label suele ser:
    # "2020 Toyota Hilux, S/65,000, Juliaca, publicación 123".
    # Algunas tarjetas omiten el título y empiezan directamente por ", S/...".
    price_match = re.match(r"^(.*?),\s*(?:S/|US\$|USD\s*|\$|€)\s*", text, re.IGNORECASE)
    if price_match:
        text = price_match.group(1).strip(" ,")
    generic = {
        "",
        "facebook",
        "marketplace",
        "imagen",
        "image",
        "foto",
        "photo",
        "ver publicación",
        "view listing",
    }
    return "" if text.casefold() in generic else text


def safe_filename(value: str, fallback: str = "publicacion", limit: int = 90) -> str:
    value = unicodedata.normalize("NFKD", value)
    value = "".join(c for c in value if not unicodedata.combining(c))
    value = value.replace("/", "-").replace("\\", "-")
    value = re.sub(r"[^A-Za-z0-9._ -]+", "", value)
    value = re.sub(r"[\s._-]+", "_", value).strip("._-")
    return (value[:limit].rstrip("._-") or fallback).lower()


def normalized_text(value: str) -> str:
    value = unicodedata.normalize("NFKD", value).casefold()
    value = "".join(c for c in value if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", value).strip()


VEHICLE_TERMS = re.compile(
    r"\b(?:"
    r"auto|automovil|carro|camioneta|camion|minibus|microbus|bus|combi|furgon|van|"
    r"suv|sedan|hatchback|station wagon|pickup|pick up|"
    r"toyota|kia|hyundai|nissan|chevrolet|ford|volkswagen|suzuki|honda|"
    r"mitsubishi|renault|mazda|subaru|volvo|mercedes|bmw|audi|jac|jmc|"
    r"fuso|hino|isuzu|changan|great wall|geely|chery|jetour|"
    r"fiat|peugeot|citroen|jeep|dodge|ram|lexus|land rover|"
    r"range rover|daihatsu|daewoo|ssangyong|dongfeng|sinotruk|howo|scania|mack|"
    r"iveco|kenworth|freightliner|king long|joylong|yutong|dfsk|lifan|zongshen|"
    r"italika|hilux|hiace|corolla|yaris|rav4|sportage|tucson|sorento|duster"
    r")\b"
)

NON_VEHICLE_TERMS = re.compile(
    r"\b(?:"
    r"repuesto|repuestos|autoparte|autopartes|accesorio|accesorios|parabrisas|"
    r"capot|frontal|arrancador|arrancadores|compresora|bobcat|rastra|rastras|"
    r"landini|"
    r"[a-z]*moto[a-z]*|motocicleta|mototaxi|motocar|trimoto|cuatrimoto|scooter|motoneta|"
    r"torito|ssenda|mijo|kinglon|"
    r"bajaj|yamaha|cfmoto|keeway|ktm|husqvarna|tvs|zongshen|italika|"
    r"pulsar|intruder|navi|dominar|apache|cb190|cbr|r15|fz|"
    r"faro|faros|llanta|llantas|neumatico|neumaticos|tapa de rueda|arrancador|"
    r"servicio|servicios|alquiler|alquilo|cochera|terreno|lote|tramite|"
    r"tarjeta unica|eliminacion de desmonte|agregados|molino|bloquetera|"
    r"excavadora|rodillo compactador|martillo hidraulico|minicargador|"
    r"semiremolque|remolque|chatarra|para desarme|vendo motor|motor para|"
    r"motor (?:toyota|nissan|ford|chevrolet|hyundai|kia|mitsubishi|suzuki|honda)|"
    r"reparado|balanceado|original para|compra alq|alq|"
    r"cabina para|venta de cabina|compro moto|compro auto|se compra"
    r")\b"
)

AMBIGUOUS_BRAND_TERMS = re.compile(r"\b(?:honda|suzuki)\b")
CAR_CONTEXT_TERMS = re.compile(
    r"\b(?:auto|automovil|carro|camioneta|suv|sedan|hatchback|station wagon|"
    r"pickup|pick up|van|civic|accord|crv|hrv|pilot|city|fit|odyssey|ridgeline|"
    r"brv|swift|vitara|grand nomade|jimny|spresso|alto|celerio|ertiga|xl7|"
    r"baleno|sx4|carry)\b"
)


def is_vehicle_listing(listing: Listing) -> bool:
    """Filtro conservador: exige evidencia textual de un vehículo completo."""
    text = normalized_text(f"{listing.title} {listing.search_text}")
    if not text or NON_VEHICLE_TERMS.search(text):
        return False
    if AMBIGUOUS_BRAND_TERMS.search(text) and not CAR_CONTEXT_TERMS.search(text):
        return False
    return bool(VEHICLE_TERMS.search(text))


def is_visual_candidate(listing: Listing) -> bool:
    """Deja pasar títulos ambiguos a YOLO, pero bloquea exclusiones explícitas."""
    text = normalized_text(f"{listing.title} {listing.search_text}")
    return not bool(text and NON_VEHICLE_TERMS.search(text))


class VehicleDetector:
    """Detecta visualmente vehículos de cuatro o más ruedas con YOLO/COCO."""

    ALLOWED_CLASSES = {"car", "bus", "truck"}
    MOTORCYCLE_CLASS = "motorcycle"

    def __init__(self) -> None:
        from ultralytics import YOLO

        (YOLO_CONFIG_DIR / "Ultralytics").mkdir(parents=True, exist_ok=True)
        print("Cargando detector visual YOLO (la primera vez descarga un modelo pequeño)...")
        self.model = YOLO(str(MODEL_PATH))

    def accepts(self, image_path: Path, listing: Listing) -> tuple[bool, str]:
        results = self.model.predict(
            source=str(image_path),
            imgsz=640,
            conf=0.20,
            verbose=False,
        )
        result = results[0]
        image_height, image_width = result.orig_shape
        image_area = max(image_height * image_width, 1)
        allowed: list[tuple[str, float, float]] = []
        motorcycle_found = False

        if result.boxes is not None:
            classes = result.boxes.cls.cpu().tolist()
            confidences = result.boxes.conf.cpu().tolist()
            coordinates = result.boxes.xyxy.cpu().tolist()
            for class_id, confidence, (x1, y1, x2, y2) in zip(
                classes, confidences, coordinates
            ):
                label = str(result.names[int(class_id)]).casefold()
                area_ratio = max(0.0, (x2 - x1) * (y2 - y1) / image_area)
                if label == self.MOTORCYCLE_CLASS:
                    motorcycle_found = True
                if label in self.ALLOWED_CLASSES and (
                    (confidence >= 0.24 and area_ratio >= 0.10) or confidence >= 0.60
                ):
                    allowed.append((label, float(confidence), float(area_ratio)))

        # Si la imagen contiene una moto, solo aceptar otra detección simultánea
        # cuando el texto confirme claramente que el anuncio es de un auto/camión.
        if motorcycle_found and not is_vehicle_listing(listing):
            return False, "YOLO detectó una moto sin confirmación textual de automóvil"
        if allowed:
            label, confidence, _ = max(allowed, key=lambda item: item[1])
            return True, f"YOLO: {label} {confidence:.0%}"
        if motorcycle_found:
            return False, "YOLO detectó una moto y ningún auto/camión/bus"
        if is_vehicle_listing(listing):
            return True, "texto claro (respaldo cuando YOLO no detectó)"
        return False, "YOLO no detectó auto, camioneta, camión ni bus"


class VehicleClassifier:
    """Segundo filtro independiente que veta motos, maquinaria y otros objetos."""

    MOTORCYCLE_CLASSES = {
        "motor_scooter", "moped", "motorcycle", "tricycle",
        "bicycle-built-for-two", "mountain_bike",
    }
    MACHINERY_CLASSES = {
        "tractor", "harvester", "plow", "forklift", "thresher",
        "snowmobile", "golfcart", "crane_(machine)", "snowplow",
        "lawn_mower", "chain_saw",
    }
    CLEAR_NON_VEHICLE_CLASSES = {
        "web_site", "television", "photocopier", "toaster", "slot",
        "remote_control", "reflex_camera", "Polaroid_camera",
        "parking_meter", "backpack", "barber_chair",
    }
    ROAD_VEHICLE_CLASSES = {
        "minivan", "minibus", "pickup", "sports_car", "beach_wagon",
        "convertible", "jeep", "limousine", "moving_van",
        "recreational_vehicle", "garbage_truck", "racer", "trailer_truck",
        "cab", "ambulance", "police_van", "passenger_car", "tow_truck",
        "trolleybus", "fire_engine", "school_bus", "Model_T",
    }

    def __init__(self) -> None:
        from ultralytics import YOLO

        print("Cargando segundo filtro visual de clasificación...")
        self.model = YOLO(str(CLASSIFIER_MODEL_PATH))

    def accepts(self, image_path: Path) -> tuple[bool, str]:
        result = self.model.predict(
            source=str(image_path), imgsz=224, verbose=False
        )[0]
        top_indices = result.probs.top5
        confidences = result.probs.top5conf.cpu().tolist()
        predictions = [
            (str(result.names[int(index)]), float(confidence))
            for index, confidence in zip(top_indices, confidences)
        ]
        top_label, top_confidence = predictions[0]
        road_confidence = max(
            (confidence for label, confidence in predictions
             if label in self.ROAD_VEHICLE_CLASSES),
            default=0.0,
        )

        if (
            top_label in self.MOTORCYCLE_CLASSES
            and top_confidence >= 0.15
            and road_confidence < 0.10
        ):
            return False, f"segundo filtro: {top_label} {top_confidence:.0%}"
        if (
            top_label in self.MACHINERY_CLASSES
            and top_confidence >= 0.25
            and road_confidence < 0.15
        ):
            return False, f"segundo filtro: {top_label} {top_confidence:.0%}"
        if top_label in self.CLEAR_NON_VEHICLE_CLASSES and top_confidence >= 0.45:
            return False, f"segundo filtro: {top_label} {top_confidence:.0%}"
        return True, f"clasificación: {top_label} {top_confidence:.0%}"


def canonical_listing(url: str) -> tuple[str, str]:
    absolute = urljoin("https://www.facebook.com", url)
    match = re.search(r"/marketplace/item/(\d+)", absolute)
    if match:
        item_id = match.group(1)
        return item_id, f"https://www.facebook.com/marketplace/item/{item_id}/"
    parsed = urlparse(absolute)
    clean = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
    return hashlib.sha1(clean.encode("utf-8")).hexdigest()[:16], clean


def navigate_tolerantly(page: Page, url: str) -> bool:
    """Navega sin cerrar el navegador si Facebook entra en un bucle de redirección."""
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        return True
    except PlaywrightTimeoutError:
        # La página puede ser usable aunque recursos secundarios sigan cargando.
        return True
    except Exception as exc:
        message = str(exc)
        if "ERR_TOO_MANY_REDIRECTS" not in message:
            print(f"Aviso de navegación: {message.splitlines()[0]}")
        return False


def wait_for_listings(page: Page) -> None:
    print("\nFacebook se abrió en Chromium.")
    print("Si se solicita, inicia sesión y completa manualmente cualquier verificación.")
    print("No cierres la ventana. El programa continuará al detectar los anuncios.\n")

    deadline = time.monotonic() + LOGIN_WAIT_SECONDS
    last_notice = 0.0
    last_navigation_attempt = 0.0
    while time.monotonic() < deadline:
        try:
            count = page.locator('a[href*="/marketplace/item/"]').count()
            if count:
                print(f"Lista detectada ({count} publicaciones visibles inicialmente).")
                return
        except PlaywrightTimeoutError:
            pass
        except Exception:
            # Una navegación o recarga manual puede destruir temporalmente el contexto JS.
            pass

        now = time.monotonic()
        current_url = page.url.lower()
        is_manual_step = any(
            marker in current_url
            for marker in ("/login", "/checkpoint", "/two_step_verification", "/captcha")
        )
        # Nunca interrumpir mientras el usuario escribe sus credenciales o resuelve
        # 2FA/CAPTCHA. Si ya salió del inicio de sesión, sí se puede reintentar la
        # lista oficial con una pausa amplia.
        if not is_manual_step and now - last_navigation_attempt > 12:
            target = LOGIN_URL if current_url.startswith("chrome-error:") else MOBILE_VEHICLES_URL
            navigate_tolerantly(page, target)
            last_navigation_attempt = now
        if now - last_notice > 30:
            print("Esperando la lista... resuelve en el navegador el inicio de sesión o verificación.")
            last_notice = now
        page.wait_for_timeout(1_500)

    raise RuntimeError(
        "No se detectaron publicaciones después de 15 minutos. "
        "Comprueba que la categoría 'Vehículos' esté abierta y tenga anuncios visibles."
    )


EXTRACT_VISIBLE_LISTINGS_JS = r"""
() => {
  const results = [];
  const anchors = Array.from(document.querySelectorAll('a[href*="/marketplace/item/"]'));

  function srcsetCandidates(value) {
    if (!value) return [];
    return value.split(',').map(part => {
      const bits = part.trim().split(/\s+/);
      const descriptor = bits[bits.length - 1] || '';
      let score = 0;
      if (/^\d+w$/.test(descriptor)) score = parseInt(descriptor, 10);
      if (/^[\d.]+x$/.test(descriptor)) score = parseFloat(descriptor) * 1000;
      return {url: bits[0], score};
    }).filter(x => x.url);
  }

  function bestImage(container) {
    const images = Array.from(container.querySelectorAll('img'));
    const choices = [];
    for (const img of images) {
      const rect = img.getBoundingClientRect();
      const alt = (img.getAttribute('alt') || '').toLowerCase();
      const role = (img.getAttribute('role') || '').toLowerCase();
      if (rect.width < 80 || rect.height < 60) continue;
      if (role === 'presentation' && rect.width < 120) continue;
      if (/profile|perfil|avatar|emoji|icon|ícono/.test(alt)) continue;
      const baseScore = Math.max(img.naturalWidth || 0, rect.width) *
                        Math.max(img.naturalHeight || 0, rect.height);
      const urls = [
        {url: img.currentSrc, score: baseScore},
        {url: img.src, score: baseScore},
        {url: img.getAttribute('data-src'), score: baseScore},
        ...srcsetCandidates(img.getAttribute('srcset')),
        ...srcsetCandidates(img.getAttribute('data-srcset')),
      ];
      for (const candidate of urls) {
        if (!candidate.url || candidate.url.startsWith('data:')) continue;
        if (!/^https?:/i.test(candidate.url)) continue;
        choices.push({
          url: candidate.url,
          score: Math.max(candidate.score || 0, baseScore),
          width: img.naturalWidth || Math.round(rect.width),
          alt: img.getAttribute('alt') || '',
        });
      }
    }
    choices.sort((a, b) => b.score - a.score);
    return choices[0] || null;
  }

  function titleFor(anchor, card, image) {
    const candidates = [
      anchor.getAttribute('aria-label'),
      image && image.alt,
      anchor.getAttribute('title'),
    ];
    for (const node of card.querySelectorAll('[aria-label], [role="heading"], span')) {
      const text = (node.innerText || node.getAttribute('aria-label') || '').trim();
      if (text && text.length <= 180) candidates.push(text);
    }
    const blocked = /^(facebook|marketplace|imagen|image|foto|photo|ver publicación|view listing)$/i;
    return candidates.find(x => x && x.trim() && !blocked.test(x.trim())) || '';
  }

  for (const anchor of anchors) {
    const rect = anchor.getBoundingClientRect();
    if (rect.width < 80 || rect.height < 50) continue;

    let card = anchor;
    let image = bestImage(anchor);
    // En algunas variantes de Facebook, el enlace y la miniatura son hermanos.
    for (let i = 0; !image && i < 4 && card.parentElement; i++) {
      card = card.parentElement;
      if (card.querySelectorAll('a[href*="/marketplace/item/"]').length > 3) break;
      image = bestImage(card);
    }
    if (!image) continue;

    results.push({
      href: anchor.href,
      title: titleFor(anchor, card, image),
      imageUrl: image.url,
      imageWidth: image.width || 0,
      searchText: [
        anchor.getAttribute('aria-label') || '',
        anchor.getAttribute('title') || '',
        anchor.innerText || '',
        card.innerText || '',
        image.alt || '',
      ].join(' ').slice(0, 2000),
    });
  }
    return results;
}
"""


def collect_all_listings(page: Page) -> list[Listing]:
    collected: dict[str, Listing] = {}
    rejected_keys: set[str] = set()
    unchanged = 0
    previous_count = 0

    print("Cargando todas las publicaciones mediante scroll progresivo...")
    for scroll_no in range(1, MAX_SCROLLS + 1):
        try:
            rows = page.evaluate(EXTRACT_VISIBLE_LISTINGS_JS)
        except Exception as exc:
            print(f"Aviso: no se pudo leer este tramo de la lista: {exc}")
            rows = []

        for row in rows:
            try:
                key, listing_url = canonical_listing(row["href"])
                candidate = Listing(
                    item_key=key,
                    title=clean_title(row.get("title")),
                    listing_url=listing_url,
                    image_url=row["imageUrl"],
                    image_width=int(row.get("imageWidth") or 0),
                    search_text=str(row.get("searchText") or ""),
                )
                if not is_visual_candidate(candidate):
                    rejected_keys.add(key)
                    continue
                current = collected.get(key)
                if current is None or candidate.image_width > current.image_width:
                    collected[key] = candidate
            except (KeyError, TypeError, ValueError):
                continue

        count = len(collected)
        print(
            f"  Scroll {scroll_no}: {count} candidatos; {len(rejected_keys)} descartados por texto",
            end="\r",
            flush=True,
        )
        unchanged = unchanged + 1 if count == previous_count else 0
        previous_count = count

        at_bottom = page.evaluate(
            "Math.ceil(window.scrollY + window.innerHeight) >= document.documentElement.scrollHeight - 8"
        )
        if unchanged >= UNCHANGED_LIMIT and at_bottom:
            break

        page.evaluate("window.scrollBy(0, Math.max(window.innerHeight * 0.82, 650))")
        page.wait_for_timeout(SCROLL_PAUSE_MS)

    print()
    if not collected:
        raise RuntimeError(
            "No se encontraron tarjetas válidas. Asegúrate de que existan publicaciones "
            "y de que la lista esté visible en el navegador."
        )
    return list(collected.values())


def download_image(context: BrowserContext, listing: Listing, destination: Path) -> None:
    response = None
    last_error: Exception | None = None
    for attempt in range(1, 4):
        try:
            response = context.request.get(
                listing.image_url,
                headers={"Referer": "https://www.facebook.com/"},
                timeout=45_000,
            )
            if not response.ok:
                raise RuntimeError(f"HTTP {response.status}")
            break
        except Exception as exc:
            last_error = exc
            if attempt == 3:
                raise
            time.sleep(1.5 * attempt)

    if response is None:
        raise RuntimeError("no se recibió la imagen") from last_error

    content = response.body()
    try:
        with Image.open(io.BytesIO(content)) as source:
            source.load()
            image = ImageOps.exif_transpose(source)
            if getattr(image, "n_frames", 1) > 1:
                image.seek(0)
            if image.mode not in ("RGB", "L"):
                background = Image.new("RGB", image.size, "white")
                if "A" in image.getbands():
                    background.paste(image, mask=image.getchannel("A"))
                else:
                    background.paste(image.convert("RGB"))
                image = background
            elif image.mode == "L":
                image = image.convert("RGB")
            image.save(destination, "JPEG", quality=94, optimize=True)
    except UnidentifiedImageError as exc:
        raise RuntimeError("la respuesta no es una imagen válida") from exc


def write_csv(rows: list[dict[str, str | int]]) -> None:
    with CSV_PATH.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["numero", "titulo", "url_publicacion", "nombre_archivo", "url_imagen"],
        )
        writer.writeheader()
        writer.writerows(rows)


def download_all(
    context: BrowserContext,
    listings: list[Listing],
    detector: VehicleDetector,
    classifier: VehicleClassifier,
) -> tuple[int, int, int]:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    downloaded_hashes: set[str] = set()
    successful = 0
    visual_rejected = 0

    total = len(listings)
    with CSV_PATH.open("w", newline="", encoding="utf-8-sig") as csv_handle:
        writer = csv.DictWriter(
            csv_handle,
            fieldnames=["numero", "titulo", "url_publicacion", "nombre_archivo", "url_imagen"],
        )
        writer.writeheader()
        csv_handle.flush()

        for position, listing in enumerate(listings, start=1):
            title = listing.title or "publicacion"
            temp_path = OUTPUT_DIR / f".candidato_{position:05d}.jpg"
            print(f"[{position}/{total}] {title}")
            try:
                download_image(context, listing, temp_path)
                accepted, reason = detector.accepts(temp_path, listing)
                if not accepted:
                    visual_rejected += 1
                    temp_path.unlink(missing_ok=True)
                    print(f"    Omitida: {reason}.")
                    continue

                classified, classification_reason = classifier.accepts(temp_path)
                if not classified:
                    visual_rejected += 1
                    temp_path.unlink(missing_ok=True)
                    print(f"    Omitida: {classification_reason}.")
                    continue

                digest = hashlib.sha256(temp_path.read_bytes()).hexdigest()
                if digest in downloaded_hashes:
                    temp_path.unlink(missing_ok=True)
                    print("    Omitida: imagen duplicada.")
                    continue
                downloaded_hashes.add(digest)
                successful += 1
                filename = f"{successful:03d}_{safe_filename(title)}.jpg"
                destination = OUTPUT_DIR / filename
                temp_path.replace(destination)
                print(f"    Guardada ({reason}; {classification_reason}).")
                writer.writerow(
                    {
                        "numero": successful,
                        "titulo": listing.title,
                        "url_publicacion": listing.listing_url,
                        "nombre_archivo": filename,
                        "url_imagen": listing.image_url,
                    }
                )
                # Conservar un CSV válido incluso si el usuario detiene la ejecución.
                csv_handle.flush()
            except Exception as exc:
                temp_path.unlink(missing_ok=True)
                print(f"    Error en esta publicación (se continúa): {exc}")
            finally:
                temp_path.unlink(missing_ok=True)

            # Pausa corta para no descargar agresivamente.
            time.sleep(0.35)

    return successful, total, visual_rejected


def main() -> int:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    SESSION_DIR.mkdir(parents=True, exist_ok=True)

    print("Descargador de portadas de Vehículos en Facebook Marketplace")
    print(f"Salida: {OUTPUT_DIR}")

    detector = VehicleDetector()
    classifier = VehicleClassifier()

    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            user_data_dir=str(SESSION_DIR),
            headless=HEADLESS,
            viewport={"width": 1440, "height": 1000},
            locale="es-PE",
            args=["--disable-blink-features=AutomationControlled"],
        )
        context.set_default_timeout(20_000)
        page = context.pages[0] if context.pages else context.new_page()

        try:
            if not navigate_tolerantly(page, VEHICLES_URL):
                print(
                    "Facebook redirigió repetidamente la URL directa. "
                    "Se abrirá su página de acceso móvil para que inicies sesión."
                )
                navigate_tolerantly(page, LOGIN_URL)
            wait_for_listings(page)
            listings = collect_all_listings(page)
            print(f"Se detectaron {len(listings)} publicaciones. Iniciando descargas...")
            downloaded, detected, visual_rejected = download_all(
                context, listings, detector, classifier
            )
            print(f"\nProceso terminado. Se descargaron {downloaded} portadas.")
            if visual_rejected:
                print(f"YOLO descartó {visual_rejected} imágenes sin un vehículo permitido.")
            if downloaded < detected:
                other_skipped = detected - downloaded - visual_rejected
                if other_skipped:
                    print(f"Se omitieron {other_skipped} publicaciones por error o duplicado.")
            print(f"CSV: {CSV_PATH}")
            return 0
        finally:
            context.close()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nProceso cancelado por el usuario.")
        raise SystemExit(130)
    except Exception as exc:
        print(f"\nError: {exc}", file=sys.stderr)
        raise SystemExit(1)
