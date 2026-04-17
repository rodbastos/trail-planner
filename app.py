import streamlit as st
import geopandas as gpd
import folium
from streamlit_folium import st_folium
from folium.plugins import Draw, TimestampedGeoJson, Fullscreen
import os
from datetime import datetime, timedelta
import math
from urllib.parse import quote
import re
import tempfile
import io
from shapely.geometry import LineString, MultiLineString, Point
from shapely.ops import linemerge
import random
import matplotlib.pyplot as plt
import requests


class Corredor:
    """Representa um corredor percorrendo uma geometria LineString a uma velocidade constante."""
    
    def __init__(self, nome: str, geometria, velocidade_ms: float, cor: str = "#FF0000", crs_origem="EPSG:31983", sentido: int = 1):
        self.nome = nome
        self.geometria = geometria
        self.velocidade_ms = velocidade_ms
        self.cor = cor
        self.crs_origem = crs_origem
        self.sentido = 1 if int(sentido) >= 0 else -1
        self.comprimento_total = geometria.length if geometria else 0
        self.tempo_total = self.comprimento_total / velocidade_ms if velocidade_ms > 0 else 0
    
    def get_position(self, tempo_decorrido: float):
        """Retorna (lat, lon) do corredor no tempo dado. Converte de CRS de origem para EPSG:4326."""
        if not self.geometria or self.comprimento_total == 0:
            return None
        
        distancia = self.velocidade_ms * tempo_decorrido
        distancia = min(distancia, self.comprimento_total)  # para no final
        distancia_interp = distancia if self.sentido == 1 else max(self.comprimento_total - distancia, 0)
        
        # Interpolar posição ao longo da linha
        ponto = self.geometria.interpolate(distancia_interp)
        
        # Converter de CRS origem (metros) para 4326 (lat/lon)
        ponto_4326 = gpd.GeoSeries([ponto], crs=self.crs_origem).to_crs("EPSG:4326").iloc[0]
        return (ponto_4326.y, ponto_4326.x)
    
    def finalizou(self, tempo_decorrido: float) -> bool:
        return self.velocidade_ms * tempo_decorrido >= self.comprimento_total


def color_swatch_data_uri(hex_color: str) -> str:
    color = str(hex_color).strip() if hex_color else "#808080"
    if not color.startswith("#"):
        color = f"#{color}"
    safe_color = quote(color)
    svg = (
        "<svg xmlns='http://www.w3.org/2000/svg' width='18' height='18' viewBox='0 0 18 18'>"
        f"<rect x='1' y='1' width='16' height='16' rx='2' ry='2' fill='{safe_color}' stroke='%23333333' stroke-width='1'/>"
        "</svg>"
    )
    return f"data:image/svg+xml;utf8,{svg}"


def sanitize_layer_name(name: str) -> str:
    raw = str(name or "percurso").strip()
    clean = re.sub(r"[^0-9A-Za-z_]+", "_", raw).strip("_")
    return clean[:48] if clean else "percurso"


def export_percursos_to_gpkg_bytes(percursos: list, crs) -> bytes:
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".gpkg", delete=False) as tmp:
            temp_path = tmp.name

        used_layers = set()
        wrote_any = False

        for i, p in enumerate(percursos):
            geom = p.get("geometria")
            if geom is None:
                continue

            base_name = sanitize_layer_name(p.get("nome", f"percurso_{i+1}"))
            layer_name = base_name
            suffix = 1
            while layer_name in used_layers:
                suffix += 1
                layer_name = f"{base_name}_{suffix}"
            used_layers.add(layer_name)

            row = {
                "id": int(p.get("id", i)),
                "nome": str(p.get("nome", f"Percurso {i+1}")),
                "dist_m": float(p.get("comprimento_metros", 0.0)),
                "n_segs": int(p.get("num_segmentos", 0)),
                "cor": str(p.get("cor", "#000000")),
                "vel_ms": float(p.get("velocidade_ms", 0.0)),
                "inicio": str(p.get("extremidade_inicio", "A")),
                "modo": str(p.get("modo_corredor", "um")),
                "geometry": geom,
            }

            layer_gdf = gpd.GeoDataFrame([row], geometry="geometry", crs=crs)
            layer_gdf.to_file(temp_path, layer=layer_name, driver="GPKG")
            wrote_any = True

        if not wrote_any:
            raise ValueError("Nenhum percurso com geometria válida para exportar.")

        with open(temp_path, "rb") as f:
            return f.read()
    finally:
        if temp_path and os.path.exists(temp_path):
            os.remove(temp_path)

# Cores predefinidas para percursos
CORES_PERCURSOS = [
    '#e6194B', '#3cb44b', '#ffe119', '#4363d8', '#f58231',
    '#911eb4', '#42d4f4', '#f032e6', '#bfef45', '#fabed4',
    '#469990', '#dcbeff', '#9A6324', '#fffac8', '#800000',
    '#aaffc3', '#808000', '#ffd8b1', '#000075', '#808080'
]

st.set_page_config(page_title="Trail Planner - Outward Bound Brasil", layout="wide")

st.title("🗺️ Trail Planner - OBB")


def reproject_to_meters(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Reprojeta o GeoDataFrame para EPSG:31983 (SIRGAS 2000 / UTM 23S) em metros."""
    if gdf.crs is None:
        st.warning("Camada sem CRS definido. Assumindo EPSG:4326.")
        gdf = gdf.set_crs("EPSG:4326")
    
    if gdf.crs.to_epsg() != 31983:
        gdf = gdf.to_crs("EPSG:31983")
    
    return gdf


def get_layer_names(gpkg_path: str) -> list:
    """Retorna lista de camadas disponíveis no GeoPackage."""
    return gpd.list_layers(gpkg_path)["name"].tolist()


def _trail_style(_feature):
    return {'color': '#473f30', 'weight': 4, 'opacity': 0.75}


def _trail_highlight(_feature):
    return {'weight': 8, 'color': 'yellow', 'opacity': 1.0}


@st.cache_data(show_spinner=False)
def get_base_map_html(
    gdf_id: str,
    _gdf_json: str,
    bounds_tuple: tuple,
    igc_opacity: float = 0.7,
    show_trail_network: bool = True,
    show_igc: bool = True,
) -> folium.Map:
    """Cria o MAPA BASE com todos os segmentos em estilo neutro."""
    center_lat = (bounds_tuple[1] + bounds_tuple[3]) / 2
    center_lon = (bounds_tuple[0] + bounds_tuple[2]) / 2
    map_center = [center_lat, center_lon]

    m = folium.Map(location=map_center, tiles="OpenStreetMap")
    
    # Camada base - todos os segmentos em estilo neutro
    if show_trail_network:
        folium.GeoJson(
            _gdf_json,
            name="Malha de trilhas",
            style_function=_trail_style,
            highlight_function=_trail_highlight,
        ).add_to(m)

    if show_igc:
        folium.WmsTileLayer(
            url="https://datageo.ambiente.sp.gov.br/geoimage/datageoimg/ows?SERVICE=WMS",
            name="IGC 2010 TOPO 10K",
            layers="IGC_2010_TOPO_10K",
            fmt="image/png",
            transparent=True,
            opacity=igc_opacity,
            overlay=True,
            control=False,
        ).add_to(m)

    Fullscreen(
        position="topright",
        title="Tela cheia",
        title_cancel="Sair da tela cheia",
        force_separate_button=True,
    ).add_to(m)
    
    m.fit_bounds([[bounds_tuple[1], bounds_tuple[0]], [bounds_tuple[3], bounds_tuple[2]]])
    
    return m


def build_highlight_fg(gdf: gpd.GeoDataFrame, selected_ids: list = None, percursos_visiveis=None) -> folium.FeatureGroup:
    """Cria FeatureGroup com destaques dinâmicos (segmentos selecionados + percursos visíveis).
    Este FG é atualizado via feature_group_to_add sem re-render do mapa base."""
    fg = folium.FeatureGroup(name="Destaques")
    
    gdf_display = gdf.to_crs("EPSG:4326")
    
    # Segmentos selecionados (path_atual) em vermelho
    if selected_ids:
        sel_int = [int(i) for i in selected_ids]
        sel_gdf = gdf_display.loc[gdf_display.index.intersection(sel_int)]
        for _, row in sel_gdf.iterrows():
            folium.GeoJson(
                row.geometry.__geo_interface__,
                style_function=lambda x: {'color': 'red', 'weight': 6, 'opacity': 0.95},
            ).add_to(fg)
    
    # Percursos visíveis
    if percursos_visiveis:
        for percurso in percursos_visiveis:
            if not percurso.get('geometria'):
                continue
            try:
                geom_4326 = gpd.GeoSeries([percurso['geometria']], crs=gdf.crs).to_crs("EPSG:4326").iloc[0]
                cor = percurso.get('cor', 'blue')
                folium.GeoJson(
                    geom_4326.__geo_interface__,
                    style_function=lambda x, c=cor: {'color': c, 'weight': 6, 'opacity': 1.0},
                    tooltip=f"{percurso['nome']} ({percurso['comprimento_metros']:.0f}m)"
                ).add_to(fg)
            except Exception:
                pass
    
    return fg


def export_map_snapshot_bytes(
    gdf: gpd.GeoDataFrame,
    selected_ids: list = None,
    percursos_visiveis=None,
    output_format: str = "png",
    igc_opacity: float = 0.7,
    show_trail_network: bool = True,
    show_igc: bool = True,
    map_title: str = "Trail Planner OBB",
) -> bytes:
    gdf_display = gdf.to_crs("EPSG:4326")
    minx, miny, maxx, maxy = gdf_display.total_bounds
    lon_span = max(maxx - minx, 1e-6)
    lat_span = max(maxy - miny, 1e-6)
    aspect_data = lat_span / lon_span

    fig_width = 8.0
    fig_height = max(7.0, min(12.0, fig_width * aspect_data))
    fig, ax = plt.subplots(figsize=(fig_width, fig_height))

    def lonlat_to_tile(lon: float, lat: float, zoom: int):
        n = 2 ** zoom
        x = int((lon + 180.0) / 360.0 * n)
        lat_rad = math.radians(max(min(lat, 85.05112878), -85.05112878))
        y = int((1.0 - math.log(math.tan(lat_rad) + (1.0 / math.cos(lat_rad))) / math.pi) / 2.0 * n)
        return x, y

    def tile_to_lonlat(x: int, y: int, zoom: int):
        n = 2 ** zoom
        lon = x / n * 360.0 - 180.0
        lat_rad = math.atan(math.sinh(math.pi * (1.0 - 2.0 * y / n)))
        lat = math.degrees(lat_rad)
        return lon, lat

    # Fundo OSM para impressão (base map)
    try:
        target_px_width = 2200
        zoom_estimate = int(round(math.log2((360.0 * target_px_width) / (256.0 * lon_span))))
        osm_zoom = max(11, min(18, zoom_estimate))

        x_min, y_max = lonlat_to_tile(minx, miny, osm_zoom)
        x_max, y_min = lonlat_to_tile(maxx, maxy, osm_zoom)
        x_min, x_max = sorted((x_min, x_max))
        y_min, y_max = sorted((y_min, y_max))

        tile_count = (x_max - x_min + 1) * (y_max - y_min + 1)
        while tile_count > 120 and osm_zoom > 11:
            osm_zoom -= 1
            x_min, y_max = lonlat_to_tile(minx, miny, osm_zoom)
            x_max, y_min = lonlat_to_tile(maxx, maxy, osm_zoom)
            x_min, x_max = sorted((x_min, x_max))
            y_min, y_max = sorted((y_min, y_max))
            tile_count = (x_max - x_min + 1) * (y_max - y_min + 1)

        for tx in range(x_min, x_max + 1):
            for ty in range(y_min, y_max + 1):
                tile_url = f"https://tile.openstreetmap.org/{osm_zoom}/{tx}/{ty}.png"
                response = requests.get(
                    tile_url,
                    timeout=10,
                    headers={"User-Agent": "trail-planner-export/1.0"},
                )
                content_type = response.headers.get("Content-Type", "")
                if not response.ok or "image" not in content_type.lower() or not response.content:
                    continue

                tile_img = plt.imread(io.BytesIO(response.content), format="png")
                left, top = tile_to_lonlat(tx, ty, osm_zoom)
                right, bottom = tile_to_lonlat(tx + 1, ty + 1, osm_zoom)
                ax.imshow(
                    tile_img,
                    extent=[left, right, bottom, top],
                    origin="upper",
                    alpha=1.0,
                    zorder=0,
                )
    except Exception:
        pass

    if show_igc:
        try:
            wms_response = requests.get(
                "https://datageo.ambiente.sp.gov.br/geoimage/datageoimg/ows",
                params={
                    "SERVICE": "WMS",
                    "VERSION": "1.1.1",
                    "REQUEST": "GetMap",
                    "LAYERS": "IGC_2010_TOPO_10K",
                    "STYLES": "",
                    "FORMAT": "image/png",
                    "TRANSPARENT": "TRUE",
                    "SRS": "EPSG:4326",
                    "BBOX": f"{minx},{miny},{maxx},{maxy}",
                    "WIDTH": 1800,
                    "HEIGHT": 1200,
                },
                timeout=20,
            )
            if wms_response.ok and wms_response.content:
                raster = plt.imread(io.BytesIO(wms_response.content), format="png")
                ax.imshow(raster, extent=[minx, maxx, miny, maxy], origin="upper", alpha=igc_opacity, zorder=1)
        except Exception:
            pass

    if show_trail_network:
        gdf_display.plot(ax=ax, facecolor="none", edgecolor="#473f30", linewidth=1.1, alpha=0.9, zorder=2)

    if percursos_visiveis:
        for percurso in percursos_visiveis:
            geom = percurso.get("geometria")
            if geom is None:
                continue
            try:
                geom_4326 = gpd.GeoSeries([geom], crs=gdf.crs).to_crs("EPSG:4326")
                geom_4326.plot(ax=ax, color=percurso.get("cor", "blue"), linewidth=2.5, alpha=0.95, zorder=3)
            except Exception:
                pass

    ax.set_xlim(minx, maxx)
    ax.set_ylim(miny, maxy)
    ax.set_aspect("equal", adjustable="box")
    ax.set_title(map_title, fontsize=12)
    ax.set_xlabel("")
    ax.set_ylabel("")
    ax.tick_params(axis="both", which="major", labelsize=7)
    ax.xaxis.get_offset_text().set_fontsize(7)
    ax.yaxis.get_offset_text().set_fontsize(7)
    ax.grid(alpha=0.2)
    ax.text(
        0.01,
        0.01,
        "© OpenStreetMap contributors",
        transform=ax.transAxes,
        fontsize=6,
        ha="left",
        va="bottom",
        color="#222222",
        bbox={"facecolor": "white", "edgecolor": "none", "alpha": 0.65, "pad": 1.5},
    )

    buffer = io.BytesIO()
    fmt = "pdf" if output_format.lower() == "pdf" else "png"
    fig.savefig(buffer, format=fmt, dpi=220, bbox_inches="tight")
    plt.close(fig)
    buffer.seek(0)
    return buffer.getvalue()


def find_nearest_feature(gdf: gpd.GeoDataFrame, click_lat: float, click_lon: float, max_distance: float = 100):
    """Encontra a feição mais próxima do ponto clicado."""
    from shapely.geometry import Point
    
    # Criar ponto no mesmo CRS do gdf
    click_point = gpd.GeoSeries([Point(click_lon, click_lat)], crs="EPSG:4326")
    click_point = click_point.to_crs(gdf.crs).iloc[0]
    
    # Calcular distâncias
    distances = gdf.geometry.distance(click_point)
    min_idx = distances.idxmin()
    min_dist = distances.min()
    
    if min_dist <= max_distance:
        return min_idx
    return None


def concatenate_geometries(geometries):
    """Concatena uma lista de geometrias LineString em uma única LineString."""
    if not geometries:
        return None
    
    # Tentar fazer linemerge para conectar segmentos
    merged = linemerge(geometries)
    
    if isinstance(merged, LineString):
        return merged
    elif isinstance(merged, MultiLineString):
        # Se não conseguir mergear em uma linha só, retorna a MultiLineString
        return merged
    return merged


# Inicializar session_state
if "path_atual" not in st.session_state:
    st.session_state["path_atual"] = []
if "percursos_prontos" not in st.session_state:
    st.session_state["percursos_prontos"] = []
if "gdf" not in st.session_state:
    st.session_state["gdf"] = None
if "layer_name" not in st.session_state:
    st.session_state["layer_name"] = None
if "sim_anim_data" not in st.session_state:
    st.session_state["sim_anim_data"] = None
if "sim_map_key" not in st.session_state:
    st.session_state["sim_map_key"] = 0
if "source_gpkg_name" not in st.session_state:
    st.session_state["source_gpkg_name"] = None
if "main_map_initialized" not in st.session_state:
    st.session_state["main_map_initialized"] = False
if "editando_idx" not in st.session_state:
    st.session_state["editando_idx"] = None
if "map_png_bytes" not in st.session_state:
    st.session_state["map_png_bytes"] = None
if "map_pdf_bytes" not in st.session_state:
    st.session_state["map_pdf_bytes"] = None
if "export_state_signature" not in st.session_state:
    st.session_state["export_state_signature"] = None
if "show_upload_panel" not in st.session_state:
    st.session_state["show_upload_panel"] = True
if "uploaded_gpkg_bytes" not in st.session_state:
    st.session_state["uploaded_gpkg_bytes"] = None
if "uploaded_gpkg_name" not in st.session_state:
    st.session_state["uploaded_gpkg_name"] = None


if st.session_state["gdf"] is None:
    st.session_state["show_upload_panel"] = True

if st.session_state["show_upload_panel"]:
    uploaded_file = st.file_uploader(
        "Suba um arquivo GeoPackage (.gpkg) com uma rede de trilhas mapeadas",
        type=["gpkg"],
    )

    if uploaded_file is not None:
        st.session_state["uploaded_gpkg_bytes"] = uploaded_file.getvalue()
        st.session_state["uploaded_gpkg_name"] = uploaded_file.name

    file_bytes = st.session_state.get("uploaded_gpkg_bytes")
    file_name = st.session_state.get("uploaded_gpkg_name")

    if file_bytes is not None and file_name is not None:
        loaded_layer = False
        # Salvar arquivo temporariamente
        temp_path = f"temp_{file_name}"
        with open(temp_path, "wb") as f:
            f.write(file_bytes)

        try:
            # Listar camadas disponíveis
            layers = get_layer_names(temp_path)

            if not layers:
                st.error("Nenhuma camada encontrada no arquivo.")
            else:
                # Seleção da camada
                current_layer = st.session_state.get("layer_name")
                default_layer_idx = layers.index(current_layer) if current_layer in layers else 0
                selected_layer = st.selectbox(
                    "Seleciona a camada onde estão as trilhas",
                    layers,
                    index=default_layer_idx,
                )

                # Botão para carregar
                if st.button("📥 Carregar Camada"):
                    with st.spinner("Carregando dados..."):
                        # Ler camada com geopandas
                        gdf = gpd.read_file(temp_path, layer=selected_layer)

                        # Reprojetar para metros (EPSG:31983 - SIRGAS 2000)
                        gdf = reproject_to_meters(gdf)

                        # Adicionar índice como coluna para identificação
                        gdf = gdf.reset_index(drop=True)
                        gdf["feature_id"] = gdf.index.astype(str)

                        # Armazenar no session_state
                        st.session_state["gdf"] = gdf
                        st.session_state["layer_name"] = selected_layer
                        st.session_state["source_gpkg_name"] = file_name
                        st.session_state["path_atual"] = []
                        st.session_state["main_map_initialized"] = False
                        st.session_state["show_upload_panel"] = False
                        loaded_layer = True

                        st.success(f"✅ Camada '{selected_layer}' carregada! CRS: {gdf.crs}")

            # Limpar arquivo temporário
            os.remove(temp_path)
            if loaded_layer:
                st.rerun()

        except Exception as e:
            st.error(f"Erro ao processar arquivo: {e}")
            if os.path.exists(temp_path):
                os.remove(temp_path)

# Exibir mapa se houver dados no session_state
if st.session_state["gdf"] is not None:
    import pandas as pd
    gdf = st.session_state["gdf"]
    
    # ==================== LAYOUT PRINCIPAL ====================
    col_mapa = st.container()
    
    # ---------- COLUNA DO MAPA ----------
    with col_mapa:
        # Determinar quais percursos visualizar (coluna "ver" da tabela)
        # Se estiver editando um percurso, ocultar temporariamente sua linha salva
        # para evitar sobreposição visual com os segmentos selecionados.
        editando_idx = st.session_state.get("editando_idx")
        percursos_visiveis = [
            p for i, p in enumerate(st.session_state["percursos_prontos"])
            if p.get("visivel", False) and i != editando_idx
        ]
        
        # Status bar
        n_segs = len(st.session_state["path_atual"])
        path_length = 0.0
        if n_segs > 0:
            path_gdf = gdf.loc[st.session_state["path_atual"]]
            path_length = path_gdf.geometry.length.sum()
        
        header_col, action_col = st.columns([5, 1.4])
        header_col.markdown(
            f"#### Camada {st.session_state.get('layer_name', '-') } de {st.session_state.get('source_gpkg_name', '-') }"
        )
        if action_col.button("Trocar arquivo/camada", use_container_width=True):
            st.session_state["show_upload_panel"] = True
            st.rerun()

        # ---------- DASHBOARD (linha única) ----------
        source_name = st.session_state.get("source_gpkg_name") or "arquivo.gpkg"
        source_stem = os.path.splitext(source_name)[0]
        export_title = f"Trail Planner OBB - {source_stem}"

        st.markdown("""
        <style>
        [data-testid="stMetricValue"] { font-size: 0.95rem !important; }
        [data-testid="stMetricLabel"] { font-size: 0.7rem !important; }
        </style>
        """, unsafe_allow_html=True)
        with st.container(border=True):
            c_seg, c_dist, c_perc, c_malha, c_igc, c_slider, c_png, c_pdf = st.columns(
                [0.9, 1.1, 0.9, 0.8, 0.9, 1.6, 1.1, 1.1]
            )

            total_segs = len(gdf)
            total_dist = gdf.geometry.length.sum()
            c_seg.metric("Total de Segmentos", total_segs)
            c_dist.metric("Distância total", f"{total_dist:,.0f} m")
            c_perc.metric("Percursos salvos", len(st.session_state["percursos_prontos"]))

            mostrar_malha = c_malha.checkbox("Trilhas", value=True)
            mostrar_igc   = c_igc.checkbox("IGC 10K", value=True)
            opacidade_igc = c_slider.slider(
                "Opacidade IGC",
                min_value=0.0, max_value=1.0, value=0.7, step=0.05,
                disabled=not mostrar_igc,
                label_visibility="collapsed",
            )

            visible_ids = tuple(sorted(int(p.get("id", -1)) for p in percursos_visiveis if p.get("id") is not None))
            export_signature = (
                source_stem,
                round(float(opacidade_igc), 3),
                bool(mostrar_malha),
                bool(mostrar_igc),
                visible_ids,
            )
            if st.session_state.get("export_state_signature") != export_signature:
                st.session_state["map_png_bytes"] = None
                st.session_state["map_pdf_bytes"] = None
                st.session_state["export_state_signature"] = export_signature

            with c_png:
                if st.session_state.get("map_png_bytes"):
                    st.download_button("⬇️ PNG", data=st.session_state["map_png_bytes"],
                        file_name="mapa_percursos.png", mime="image/png", use_container_width=True)
                else:
                    if st.button("🖼️ Gerar PNG", use_container_width=True):
                        with st.spinner("Gerando..."):
                            st.session_state["map_png_bytes"] = export_map_snapshot_bytes(
                                gdf, selected_ids=None, percursos_visiveis=percursos_visiveis,
                                output_format="png", igc_opacity=opacidade_igc,
                                show_trail_network=mostrar_malha, show_igc=mostrar_igc,
                                map_title=export_title,
                            )
                        st.rerun()

            with c_pdf:
                if st.session_state.get("map_pdf_bytes"):
                    st.download_button("⬇️ PDF", data=st.session_state["map_pdf_bytes"],
                        file_name="mapa_percursos.pdf", mime="application/pdf", use_container_width=True)
                else:
                    if st.button("📄 Gerar PDF", use_container_width=True):
                        with st.spinner("Gerando..."):
                            st.session_state["map_pdf_bytes"] = export_map_snapshot_bytes(
                                gdf, selected_ids=None, percursos_visiveis=percursos_visiveis,
                                output_format="pdf", igc_opacity=opacidade_igc,
                                show_trail_network=mostrar_malha, show_igc=mostrar_igc,
                                map_title=export_title,
                            )
                        st.rerun()
        
        # Criar mapa BASE (cacheado - não re-renderiza a cada interação)
        selected_ids = [str(idx) for idx in st.session_state.get("path_atual", [])]
        gdf_display = gdf.to_crs("EPSG:4326")
        bounds_tuple = tuple(gdf_display.total_bounds)
        layer_key = st.session_state.get("layer_name", "default")
        base_map = get_base_map_html(
            layer_key,
            gdf_display.to_json(),
            bounds_tuple,
            igc_opacity=opacidade_igc,
            show_trail_network=mostrar_malha,
            show_igc=mostrar_igc,
        )
        
        # FeatureGroup com destaques (atualizado SEM re-render do mapa)
        highlight_fg = build_highlight_fg(gdf, selected_ids, percursos_visiveis)
        
        # st_folium com feature_group_to_add: só atualiza o FG, preserva zoom/pan
        map_data = st_folium(
            base_map,
            feature_group_to_add=highlight_fg,
            width=None,
            height=550,
            returned_objects=["last_clicked"],
            use_container_width=True,
            key="main_map",
        )
        st.session_state["main_map_initialized"] = True
        
        # Processar clique para adicionar segmento ao percurso atual
        if map_data and map_data.get("last_clicked"):
            click = map_data["last_clicked"]
            click_lat = click.get("lat")
            click_lng = click.get("lng")
            
            # Evitar processar o mesmo clique 2x
            last_click_key = (click_lat, click_lng)
            if click_lat is not None and click_lng is not None and st.session_state.get("last_processed_click") != last_click_key:
                st.session_state["last_processed_click"] = last_click_key
                nearest_idx = find_nearest_feature(gdf, click_lat, click_lng, max_distance=300)
                if nearest_idx is not None:
                    # Normalizar para int Python padrão (evita mismatch numpy.int64 vs int)
                    nearest_idx = int(nearest_idx)
                    path_atual_int = [int(i) for i in st.session_state["path_atual"]]
                    # Toggle: se já está no path, remove; senão, adiciona
                    if nearest_idx in path_atual_int:
                        path_atual_int.remove(nearest_idx)
                    else:
                        path_atual_int.append(nearest_idx)
                    st.session_state["path_atual"] = path_atual_int
                    st.rerun()
    
    # ---------- TABELA ABAIXO DO MAPA ----------
    with st.container():
        # ===== CRIAR / EDITAR PERCURSO =====
        editando = st.session_state["editando_idx"] is not None
        
        with st.container(border=True):
            if editando:
                p_edit = st.session_state["percursos_prontos"][st.session_state["editando_idx"]]
                st.markdown(f"#### ✏️ Editando: **{p_edit['nome']}**")
                st.caption("Clique no mapa para adicionar/remover segmentos. A geometria será recalculada ao salvar.")
            else:
                st.markdown("#### ➕ Novo Percurso")
            
            if n_segs == 0:
                st.caption("💡 Clique no mapa em segmentos para compor um novo percurso")
            else:
                default_nome = p_edit["nome"] if editando else ""
                placeholder_nome = p_edit["nome"] if editando else f"Percurso {len(st.session_state['percursos_prontos']) + 1}"
                
                sub_c1, sub_c2 = st.columns([3, 1])
                with sub_c1:
                    novo_nome = st.text_input(
                        "Nome do percurso",
                        value=default_nome,
                        placeholder=placeholder_nome,
                        key=f"input_nome_percurso_{st.session_state['editando_idx']}",
                        label_visibility="collapsed"
                    )
                with sub_c2:
                    if st.button("🗑️", help="Limpar seleção atual", use_container_width=True):
                        st.session_state["path_atual"] = []
                        st.rerun()
                
                btn_label = f"💾 Atualizar ({n_segs} segs, {path_length:,.0f}m)" if editando else f"✅ Salvar ({n_segs} segs, {path_length:,.0f}m)"
                if st.button(btn_label, type="primary", use_container_width=True):
                    nome_final = novo_nome.strip() if novo_nome.strip() else placeholder_nome
                    geometries = list(path_gdf.geometry.values)
                    concatenated_geom = concatenate_geometries(geometries)
                    
                    if editando:
                        # Atualizar percurso existente (mantém id, cor, visivel)
                        p_edit["nome"] = nome_final
                        p_edit["indices"] = st.session_state["path_atual"].copy()
                        p_edit["geometria"] = concatenated_geom
                        p_edit["comprimento_metros"] = path_length
                        p_edit["num_segmentos"] = n_segs
                        p_edit["extremidade_inicio"] = p_edit.get("extremidade_inicio", "A")
                        p_edit["modo_corredor"] = p_edit.get("modo_corredor", "um")
                        st.session_state["editando_idx"] = None
                    else:
                        cor_idx = len(st.session_state["percursos_prontos"]) % len(CORES_PERCURSOS)
                        novo_percurso = {
                            "id": len(st.session_state["percursos_prontos"]),
                            "nome": nome_final,
                            "indices": st.session_state["path_atual"].copy(),
                            "geometria": concatenated_geom,
                            "comprimento_metros": path_length,
                            "num_segmentos": n_segs,
                            "cor": CORES_PERCURSOS[cor_idx],
                            "visivel": True,
                            "velocidade_ms": 3.0 / 3.6,
                            "extremidade_inicio": "A",
                            "modo_corredor": "um",
                        }
                        st.session_state["percursos_prontos"].append(novo_percurso)
                    st.session_state["path_atual"] = []
                    st.rerun()
            
            # Botão de cancelar edição
            if editando:
                if st.button("❌ Cancelar Edição", use_container_width=True):
                    st.session_state["editando_idx"] = None
                    st.session_state["path_atual"] = []
                    st.rerun()
        
        # ===== TABELA DE PERCURSOS =====
        st.markdown("#### 📋 Percursos Salvos")
        
        if not st.session_state["percursos_prontos"]:
            st.info("Nenhum percurso salvo ainda.")
        else:
            # Montar DataFrame com colunas de ação (editar + deletar)
            percursos_df = pd.DataFrame([
                {
                    "visivel": p.get("visivel", False),
                    "nome": p["nome"],
                    "amostra": color_swatch_data_uri(p["cor"]),
                    "cor": p["cor"],
                    "distancia_m": round(p["comprimento_metros"], 1),
                    "velocidade_kmh": round(p.get("velocidade_ms", 3.0) * 3.6, 1),
                    "inicio": p.get("extremidade_inicio", "A"),
                    "corredores": p.get("modo_corredor", "um"),
                    "segmentos": p["num_segmentos"],
                    "editar": False,
                    "deletar": False,
                }
                for p in st.session_state["percursos_prontos"]
            ])
            
            # Editor reativo - detecta mudanças automaticamente
            edited_df = st.data_editor(
                percursos_df,
                column_config={
                    "visivel": st.column_config.CheckboxColumn(
                        "👁️ Ver",
                        help="Marque para exibir no mapa",
                        default=False,
                        width="small",
                    ),
                    "nome": st.column_config.TextColumn(
                        "Nome",
                        help="Clique 2x para editar",
                        required=True,
                        width="medium",
                    ),
                    "amostra": st.column_config.ImageColumn(
                        "Cor",
                        help="Amostra visual da cor do percurso",
                        width="small",
                    ),
                    "cor": st.column_config.TextColumn(
                        "Hex",
                        help="Hex (#RRGGBB). Clique 2x para editar",
                        required=True,
                        width="small",
                    ),
                    "distancia_m": st.column_config.NumberColumn(
                        "Dist (m)",
                        disabled=True,
                        format="%.0f",
                        width="small",
                    ),
                    "velocidade_kmh": st.column_config.NumberColumn(
                        "Vel (km/h)",
                        help="Velocidade do corredor em km/h (para simulação)",
                        min_value=0.5,
                        max_value=50.0,
                        step=0.5,
                        format="%.1f",
                        width="small",
                    ),
                    "inicio": st.column_config.SelectboxColumn(
                        "Início",
                        help="Extremidade onde o corredor inicia",
                        options=["A", "B"],
                        required=True,
                        width="small",
                    ),
                    "corredores": st.column_config.SelectboxColumn(
                        "Patrols",
                        help="'um' = 1 corredor | 'dois' = um em cada extremidade",
                        options=["um", "dois"],
                        required=True,
                        width="small",
                    ),
                    "segmentos": st.column_config.NumberColumn(
                        "Segs",
                        disabled=True,
                        width="small",
                    ),
                    "editar": st.column_config.CheckboxColumn(
                        "✏️",
                        help="Marque para carregar os segmentos no mapa e editar",
                        default=False,
                        width="small",
                    ),
                    "deletar": st.column_config.CheckboxColumn(
                        "🗑️",
                        help="Marque para deletar e clique no botão abaixo",
                        default=False,
                        width="small",
                    ),
                },
                hide_index=True,
                num_rows="fixed",
                use_container_width=True,
                key="editor_percursos",
            )
            
            # ===== CARREGAR PARA EDIÇÃO (única linha marcada) =====
            marcados_editar = edited_df[edited_df["editar"] == True].index.tolist()
            if marcados_editar and not editando:
                idx_edit = marcados_editar[0]
                p_ed = st.session_state["percursos_prontos"][idx_edit]
                st.session_state["editando_idx"] = idx_edit
                st.session_state["path_atual"] = list(p_ed["indices"])
                st.rerun()
            
            # ===== APLICAR MUDANÇAS REATIVAMENTE =====
            # Detectar mudanças em visivel/nome/cor e auto-aplicar
            changed = False
            for i, row in edited_df.iterrows():
                if i >= len(st.session_state["percursos_prontos"]):
                    continue
                p = st.session_state["percursos_prontos"][i]
                if bool(row["visivel"]) != bool(p.get("visivel", False)):
                    p["visivel"] = bool(row["visivel"])
                    changed = True
                if str(row["nome"]) != str(p["nome"]):
                    p["nome"] = str(row["nome"])
                    changed = True
                if str(row["cor"]) != str(p["cor"]):
                    p["cor"] = str(row["cor"])
                    changed = True
                nova_vel_ms = float(row["velocidade_kmh"]) / 3.6
                if abs(nova_vel_ms - float(p.get("velocidade_ms", 3.0))) > 0.01:
                    p["velocidade_ms"] = nova_vel_ms
                    changed = True
                inicio_val = str(row.get("inicio", "A")).upper()
                inicio_val = "A" if inicio_val not in ["A", "B"] else inicio_val
                if inicio_val != str(p.get("extremidade_inicio", "A")):
                    p["extremidade_inicio"] = inicio_val
                    changed = True
                corredores_val = str(row.get("corredores", "um")).lower()
                corredores_val = "dois" if corredores_val == "dois" else "um"
                if corredores_val != str(p.get("modo_corredor", "um")):
                    p["modo_corredor"] = corredores_val
                    changed = True
            
            if changed:
                st.rerun()
            
            # ===== BOTÕES DE AÇÃO =====
            marcados_para_deletar = edited_df[edited_df["deletar"] == True].index.tolist()
            
            btn_cols = st.columns([2, 1, 1, 2])
            with btn_cols[0]:
                disabled_del = len(marcados_para_deletar) == 0
                label = f"🗑️ Excluir ({len(marcados_para_deletar)})" if marcados_para_deletar else "🗑️ Excluir marcados"
                if st.button(label, type="primary", use_container_width=True, disabled=disabled_del):
                    # Remover em ordem decrescente para manter índices válidos
                    for idx in sorted(marcados_para_deletar, reverse=True):
                        st.session_state["percursos_prontos"].pop(idx)
                    # Reordenar IDs
                    for idx, p in enumerate(st.session_state["percursos_prontos"]):
                        p["id"] = idx
                    st.rerun()
            
            with btn_cols[1]:
                if st.button("👁️ Todos", help="Exibir todos no mapa", use_container_width=True):
                    for p in st.session_state["percursos_prontos"]:
                        p["visivel"] = True
                    st.rerun()
            
            with btn_cols[2]:
                if st.button("🚫 Ocultar", help="Ocultar todos", use_container_width=True):
                    for p in st.session_state["percursos_prontos"]:
                        p["visivel"] = False
                    st.rerun()

            with btn_cols[3]:
                try:
                    gpkg_bytes = export_percursos_to_gpkg_bytes(st.session_state["percursos_prontos"], gdf.crs)
                    st.download_button(
                        "📦 Exportar .gpkg",
                        data=gpkg_bytes,
                        file_name="percursos_exportados.gpkg",
                        mime="application/geopackage+sqlite3",
                        use_container_width=True,
                    )
                except Exception as e:
                    st.button("📦 Exportar .gpkg", disabled=True, use_container_width=True)
                    st.caption(f"Exportação indisponível: {e}")
            
            st.caption(
                "💡 **Ver no mapa**: marque 👁️ | **Renomear/Cor**: clique 2x na célula | "
                "**Editar segmentos**: marque ✏️ (carrega no mapa) | **Deletar**: marque 🗑️ + botão Excluir"
            )
    
    # ==================== SIMULADOR DE CORREDORES ====================
    st.divider()
    st.markdown("### 🏃 Simulador de Patrols")
    
    if not st.session_state["percursos_prontos"]:
        st.info("💾 Salve pelo menos um percurso para simular Patrols.")
    else:
        sim_c3, sim_c4 = st.columns([1, 1])

        with sim_c3:
            # Info: tempos reais estimados
            tempos = []
            for p in st.session_state["percursos_prontos"]:
                vel = p.get("velocidade_ms", 3.0)
                if vel > 0 and p.get("comprimento_metros", 0) > 0:
                    tempos.append(p["comprimento_metros"] / vel)
            if tempos:
                max_t = max(tempos)
                st.metric("⏱️ Tempo real total", f"{max_t/60:.1f} min", help="Tempo real até o último corredor terminar")
        
        with sim_c4:
            sim_btn_c1, sim_btn_c2 = st.columns(2)
            with sim_btn_c1:
                iniciar = st.button("🧩 Criar", type="primary", use_container_width=True, help="Monta/atualiza a simulação no mapa")
            with sim_btn_c2:
                parar = st.button("🧹 Limpar", use_container_width=True, help="Remove a simulação do mapa")

        if parar:
            st.session_state["sim_anim_data"] = None
            st.session_state["sim_map_key"] += 1
            st.rerun()
        
        if iniciar:
            # Criar corredores a partir dos percursos
            corredores = []
            for p in st.session_state["percursos_prontos"]:
                if p.get("geometria") and p.get("velocidade_ms", 0) > 0:
                    inicio = str(p.get("extremidade_inicio", "A")).upper()
                    sentido_inicio = 1 if inicio == "A" else -1
                    modo = str(p.get("modo_corredor", "um")).lower()

                    if modo == "dois":
                        corredores.append(Corredor(
                            nome=f"{p['nome']} (A→B)",
                            geometria=p["geometria"],
                            velocidade_ms=p["velocidade_ms"],
                            cor=p["cor"],
                            crs_origem=str(gdf.crs),
                            sentido=1,
                        ))
                        corredores.append(Corredor(
                            nome=f"{p['nome']} (B→A)",
                            geometria=p["geometria"],
                            velocidade_ms=p["velocidade_ms"],
                            cor=p["cor"],
                            crs_origem=str(gdf.crs),
                            sentido=-1,
                        ))
                    else:
                        corredores.append(Corredor(
                            nome=f"{p['nome']} ({inicio})",
                            geometria=p["geometria"],
                            velocidade_ms=p["velocidade_ms"],
                            cor=p["cor"],
                            crs_origem=str(gdf.crs),
                            sentido=sentido_inicio,
                        ))
            
            if not corredores:
                st.warning("Nenhum corredor válido para simular.")
            else:
                tempo_max = max(c.tempo_total for c in corredores)
                if tempo_max <= 0:
                    st.warning("Não foi possível calcular a duração da simulação.")
                else:
                    # Centro do mapa a partir dos bounds do gdf
                    gdf_4326 = gdf.to_crs("EPSG:4326")
                    bounds = gdf_4326.total_bounds
                    center = [(bounds[1] + bounds[3]) / 2, (bounds[0] + bounds[2]) / 2]

                    # Discretização temporal estável: no máximo ~600 amostras por corredor.
                    step_sim_s = max(1, int(math.ceil(tempo_max / 600.0)))
                    tempos_sim = list(range(0, int(math.ceil(tempo_max)) + 1, step_sim_s))
                    if tempos_sim[-1] != int(math.ceil(tempo_max)):
                        tempos_sim.append(int(math.ceil(tempo_max)))

                    # Trilhas de referência
                    linhas_referencia = []
                    for c in corredores:
                        try:
                            geom_4326 = gpd.GeoSeries([c.geometria], crs=c.crs_origem).to_crs("EPSG:4326").iloc[0]
                            linhas_referencia.append({
                                "geojson": geom_4326.__geo_interface__,
                                "cor": c.cor,
                                "tooltip": f"{c.nome} ({c.velocidade_ms*3.6:.1f} km/h)",
                            })
                        except Exception:
                            pass

                    # Features temporais dos corredores
                    # Base fixa para o controle temporal do mapa exibir tempo decorrido real
                    # em formato HH:mm:ss iniciando em 00:00:00.
                    start_time = datetime(2000, 1, 1, 0, 0, 0)
                    temporal_features = []
                    for c in corredores:
                        for t_sim in tempos_sim:
                            pos = c.get_position(float(t_sim))
                            if pos is None:
                                continue
                            ts = (start_time + timedelta(seconds=t_sim)).isoformat()
                            temporal_features.append({
                                "type": "Feature",
                                "geometry": {
                                    "type": "Point",
                                    "coordinates": [pos[1], pos[0]],
                                },
                                "properties": {
                                    "time": ts,
                                    "popup": f"{c.nome} - {c.velocidade_ms*3.6:.1f} km/h",
                                    "icon": "circle",
                                    "iconstyle": {
                                        "fillColor": c.cor,
                                        "fillOpacity": 0.95,
                                        "stroke": "true",
                                        "radius": 7,
                                        "color": "#FFFFFF",
                                        "weight": 2,
                                    },
                                },
                            })

                    st.session_state["sim_anim_data"] = {
                        "center": center,
                        "bounds": bounds.tolist(),
                        "step_sim_s": step_sim_s,
                        "transition_ms": 250,
                        "tempo_max": tempo_max,
                        "num_corredores": len(corredores),
                        "linhas_referencia": linhas_referencia,
                        "features": temporal_features,
                    }
                    st.session_state["sim_map_key"] += 1

        sim_data = st.session_state.get("sim_anim_data")
        if sim_data:
            sim_map = folium.Map(location=sim_data["center"], zoom_start=13, tiles="OpenStreetMap")
            b = sim_data["bounds"]
            sim_map.fit_bounds([[b[1], b[0]], [b[3], b[2]]])

            for linha in sim_data["linhas_referencia"]:
                folium.GeoJson(
                    linha["geojson"],
                    style_function=lambda x, col=linha["cor"]: {'color': col, 'weight': 4, 'opacity': 0.55},
                    tooltip=linha["tooltip"],
                ).add_to(sim_map)

            TimestampedGeoJson(
                {
                    "type": "FeatureCollection",
                    "features": sim_data["features"],
                },
                period=f"PT{sim_data['step_sim_s']}S",
                add_last_point=True,
                auto_play=True,
                loop=False,
                speed_slider=True,
                max_speed=10,
                loop_button=True,
                date_options="HH:mm:ss",
                time_slider_drag_update=True,
                duration=f"PT{sim_data['step_sim_s']}S",
                transition_time=sim_data["transition_ms"],
            ).add_to(sim_map)

            st.info(
                f"Simulação ativa: {sim_data['num_corredores']} Patrols | "
                f"tempo real total ~{sim_data['tempo_max']:.0f}s | passo de amostragem {sim_data['step_sim_s']}s"
            )
            st_folium(
                sim_map,
                width=None,
                height=550,
                use_container_width=True,
                key=f"sim_map_{st.session_state['sim_map_key']}",
            )
