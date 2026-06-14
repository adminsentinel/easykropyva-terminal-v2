# EasyKropyva Terminal Backend v1.6.2 - Simplified elevation profile
import math
import time
import os
import sys
import threading
import asyncio
from pathlib import Path
from flask import Flask, render_template, jsonify, request, Response
from flask_cors import CORS
import requests

application = Flask(__name__)  # Render requires this variable name
CORS(application)

# Глобальні дані (сховище в пам'яті)
nodes = {}
targets = {}
home_point = None

# --- ГЕОМЕТРІЯ ТА ФІЗИКА ---

def haversine_distance(lat1, lng1, lat2, lng2):
    """Розрахунок відстані між точками (метри)"""
    R = 6371000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlamb = math.radians(lng2 - lng1)
    a = math.sin(dphi/2)**2 + math.cos(phi1)*math.cos(phi2)*math.sin(dlamb/2)**2
    return 2 * R * math.atan2(math.sqrt(a), math.sqrt(1-a))

def sample_line(lat1, lng1, lat2, lng2, samples=30):
    """Генерує список точок на лінії між A та B"""
    points = []
    for i in range(samples):
        f = i / (samples - 1)
        lat = lat1 + (lat2 - lat1) * f
        lng = lng1 + (lng2 - lng1) * f
        points.append((lat, lng))
    return points

def calculate_diffraction_loss(h, d1, d2, freq_mhz):
    """
    Спрощений розрахунок втрат на дифракцію (Knife-edge).
    h: висота перешкоди над лінією LOS (може бути від'ємною).
    """
    if h <= -0.6: return 0.0
    v = h * math.sqrt(2 * (d1 + d2) / ( (300 / freq_mhz) * d1 * d2 ))
    if v <= -1: return 0.0
    return 6.9 + 20 * math.log10(math.sqrt((v - 0.1)**2 + 1) + v - 0.1)

def compute_los(elevations, base_height_a=0.5, base_height_b=0.5, freq_mhz=433.0, distance_m=0, vegetation_height_m=0.5):
    """
    Розрахунок прямої видимості та зони Френеля.
    Додано модель затухання трави (ITU-R P.833 спрощена).
    """
    n = len(elevations)
    if n < 2: return {'пряма_видимість': True, 'terrain_los': True}

    # Модель затухання трави (спрощена ITU-R P.833)
    # Для 433 MHz: ~0.15 dB/m, для 900 MHz: ~0.3 dB/m
    veg_attenuation_db_per_m = 0.15 if freq_mhz < 500 else 0.3
    vegetation_loss = veg_attenuation_db_per_m * vegetation_height_m * (distance_m / 1000.0) if distance_m > 0 else 0

    elev_a = elevations[0] + base_height_a
    elev_b = elevations[-1] + base_height_b

    # Копіюємо список, щоб не мутувати оригінальні дані
    elevs = list(elevations)
    distance_km = distance_m / 1000.0
    
    for i in range(n):
        f = i / (n - 1)
        dist_from_start_km = f * distance_km
        # Висота "просідання" горизонту через кривизну
        earth_drop = (dist_from_start_km * (distance_km - dist_from_start_km)) / 12.74
        elevs[i] += earth_drop

    los_clear = True
    fresnel_clear = True
    max_obstruction = 0.0
    obstruction_idx = -1

    for i in range(1, n - 1):
        f = i / (n - 1)
        line_height = elev_a + f * (elev_b - elev_a)
        terrain = elevs[i]

        # Оптична видимість (Чи рельєф перекриває пряму лінію)
        if terrain > line_height:
            los_clear = False
            diff = terrain - line_height
            if diff > max_obstruction:
                max_obstruction = diff
                obstruction_idx = i

        # Зона Френеля (60%)
        if distance_m > 0 and freq_mhz > 0:
            d1_km = (f * distance_m) / 1000.0
            d2_km = ((1 - f) * distance_m) / 1000.0
            f_ghz = freq_mhz / 1000.0
            fresnel_radius = 17.32 * math.sqrt((d1_km * d2_km) / (f_ghz * (distance_m / 1000.0)))
            clearance_required = 0.6 * fresnel_radius
            
            if terrain > (line_height - clearance_required):
                fresnel_clear = False

    # Розрахунок бюджету лінку (RSSI)
    d_km = max(0.01, distance_m / 1000.0)
    fspl = 20 * math.log10(d_km) + 20 * math.log10(freq_mhz) + 32.44
        
    diffraction_loss = 0.0
    if not los_clear and obstruction_idx > 0:
        f = obstruction_idx / (n - 1)
        d1 = f * distance_m
        d2 = (1 - f) * distance_m
        line_at_obs = elev_a + f * (elev_b - elev_a)
        h_obs = elevs[obstruction_idx] - line_at_obs
        diffraction_loss = calculate_diffraction_loss(h_obs, d1, d2, freq_mhz)

    total_path_loss = fspl + diffraction_loss + vegetation_loss

    # Створення профілів для графіку
    terrain_profile = [round(e, 1) for e in elevs]
    los_beam = []
    fresnel_60 = []

    for i in range(n):
        f = i / (n - 1)
        line_height = elev_a + f * (elev_b - elev_a)
        los_beam.append(round(line_height, 1))
        
        d1_km = (f * distance_m) / 1000.0
        d2_km = ((1 - f) * distance_m) / 1000.0
        fresnel_radius = 17.32 * math.sqrt((d1_km * d2_km) / ((freq_mhz/1000.0) * (distance_m/1000.0))) if distance_m > 0 else 0
        fresnel_60.append(round(line_height - 0.6 * fresnel_radius, 1))

    radio_horizon_km = 4.12 * (math.sqrt(base_height_a) + math.sqrt(base_height_b))
    is_beyond_horizon = (distance_m / 1000.0) > radio_horizon_km

    return {
        'пряма_видимість': los_clear and not is_beyond_horizon,  # Лояльніший критерій для mesh network
        'terrain_los': los_clear,
        'fresnel_los': fresnel_clear,
        'beyond_horizon': is_beyond_horizon,
        'radio_horizon_km': round(radio_horizon_km, 2),
        'статус': 'ВИДИМІСТЬ Є' if (los_clear and fresnel_clear and not is_beyond_horizon) else
                  ('ЗА ГОРИЗОНТОМ' if is_beyond_horizon else
                  (f"БЛОКОВАНО РЕЛЬЄФОМ (-{round(max_obstruction, 1)}м)" if not los_clear else "ЧАСТКОВЕ БЛОКУВАННЯ (ФРЕНЕЛЬ)")),
        'висота_початку_рельєф': round(elevs[0], 1),
        'висота_кінця_рельєф': round(elevs[-1], 1),
        'path_loss_db': round(total_path_loss, 1),
        'fspl_db': round(fspl, 1),
        'diffraction_loss_db': round(diffraction_loss, 1),
        'vegetation_loss_db': round(vegetation_loss, 1),
        'terrain_profile': terrain_profile,
        'los_beam': los_beam,
        'fresnel_60': fresnel_60,
        'необхідний_підйом_м': round(max_obstruction, 1) if not los_clear else 0.0,
        'відстань_м': round(distance_m, 1)
    }

def generate_synthetic_elevation(lat1, lng1, lat2, lng2, samples=30):
    """Генерує синтетичні дані рельєфу на основі координат (як fallback)."""
    import random
    random.seed(int(abs(lat1 * 1000) + abs(lng1 * 1000)))
    base_elev = 200 + (lat1 - 48) * 50
    return [base_elev + random.uniform(-5, 5) + math.sin(i/5)*10 for i in range(samples)]

# --- ROUTES ---

@application.route('/')
def index():
    # Для Render: HTML в корені репозиторію поруч з app.py
    easyk_path = Path(__file__).parent / "easykropyva_terminal_v1_5.html"
    if easyk_path.exists():
        return Response(easyk_path.read_text(encoding="utf-8"), mimetype="text/html")
    return render_template('index.html')

@application.route('/api/los', methods=['POST'])
def api_los():
    payload = request.get_json(silent=True) or {}
    lat1 = payload.get('lat1')
    lng1 = payload.get('lng1')
    lat2 = payload.get('lat2')
    lng2 = payload.get('lng2')

    if lat1 is None or lng1 is None or lat2 is None or lng2 is None:
        return jsonify({'помилка': 'Відсутні координати'}), 400

    base_h_a = payload.get('base_height_a', 0.5)
    base_h_b = payload.get('base_height_b', 0.5)
    samples = payload.get('samples', 30)
    freq_mhz = payload.get('freq_mhz', 433.0)

    elevations = None
    try:
        coords = sample_line(lat1, lng1, lat2, lng2, samples=samples)
        loc_str = '|'.join(f'{lat},{lng}' for lat, lng in coords)
        resp = requests.get(f'https://api.open-elevation.com/api/v1/lookup?locations={loc_str}', timeout=3)
        if resp.status_code == 200:
            data = resp.json()
            elevations = [r['elevation'] for r in data.get('results', [])]
    except: pass

    if elevations is None:
        elevations = generate_synthetic_elevation(lat1, lng1, lat2, lng2, samples)

    dist_m = haversine_distance(lat1, lng1, lat2, lng2)
    result = compute_los(elevations, base_h_a, base_h_b, freq_mhz, dist_m)

    # Примусово додаємо дані профілю якщо їх немає
    if not result.get('terrain_profile'):
        result['terrain_profile'] = [round(e, 1) for e in elevations]
        result['los_beam'] = [round(elevations[0] + base_h_a + (elevations[-1] + base_h_b - elevations[0] - base_h_a) * i / len(elevations), 1) for i in range(len(elevations))]
        result['fresnel_60'] = [round(h - 5, 1) for h in result['los_beam']]

    return jsonify(result)

@application.route('/api/nodes', methods=['GET', 'POST', 'DELETE'])
def api_nodes():
    global nodes
    if request.method == 'GET':
        return jsonify(list(nodes.values()))
    elif request.method == 'POST':
        data = request.get_json()
        node_id = int(time.time() * 1000)
        data['id'] = node_id
        nodes[node_id] = data
        return jsonify(data), 201
    elif request.method == 'DELETE':
        node_id = request.args.get('id', type=int)
        if node_id in nodes: del nodes[node_id]
        return jsonify({'ok': True})

@application.route('/api/targets', methods=['GET', 'POST', 'DELETE'])
def api_targets():
    global targets
    if request.method == 'GET':
        return jsonify(list(targets.values()))
    elif request.method == 'POST':
        data = request.get_json()
        target_id = int(time.time() * 1000)
        data['id'] = target_id
        targets[target_id] = data
        return jsonify(data), 201
    elif request.method == 'DELETE':
        target_id = request.args.get('id', type=int)
        if target_id in targets: del targets[target_id]
        return jsonify({'ok': True})

@application.route('/api/mesh_topology')
def api_mesh_topology():
    mesh_nodes = [n for n in nodes.values() if n.get('type') in ['sensor', 'relay']]
    links = []
    pairs = []
    
    # Збираємо всі можливі пари
    for i in range(len(mesh_nodes)):
        for j in range(i + 1, len(mesh_nodes)):
            n1, n2 = mesh_nodes[i], mesh_nodes[j]
            dist = haversine_distance(n1['lat'], n1['lng'], n2['lat'], n2['lng'])
            if dist < 30000: # 30km max link
                pairs.append({'n1': n1, 'n2': n2, 'dist': dist})
    
    # Розраховуємо LOS для всіх пар
    for p in pairs:
        samples = 15
        elevs = generate_synthetic_elevation(p['n1']['lat'], p['n1']['lng'], p['n2']['lat'], p['n2']['lng'], samples)
        freq = 900.0 if p['n1'].get('model') == 'relay' or p['n2'].get('model') == 'relay' else 433.0
        res = compute_los(elevs, p['n1'].get('altitude_m', 0.5), p['n2'].get('altitude_m', 0.5), freq, p['dist'])
        
        # Зберігаємо розширену інформацію для кожної пари
        p['has_los'] = res['пряма_видимість']
        p['terrain_los'] = res['terrain_los']
        p['fresnel_los'] = res['fresnel_los']
        p['beyond_horizon'] = res['beyond_horizon']
    
    # Алгоритм "Best-Neighbors": для кожного вузла обираємо 2-3 найкращі зв'язки
    selected_links = set()
    max_neighbors = 3
    
    for node in mesh_nodes:
        # Знаходимо всі можливі сусіди для цього вузла
        neighbors = []
        for p in pairs:
            if p['n1']['id'] == node['id']:
                neighbors.append({'other': p['n2'], 'dist': p['dist'], 'has_los': p['has_los'], 'pair': p})
            elif p['n2']['id'] == node['id']:
                neighbors.append({'other': p['n1'], 'dist': p['dist'], 'has_los': p['has_los'], 'pair': p})
        
        # Сортуємо сусідів: спочатку за LOS (True краще), потім за відстаню (ближче краще)
        neighbors.sort(key=lambda x: (not x['has_los'], x['dist']))
        
        # Обираємо до max_neighbors найкращих
        for i, n in enumerate(neighbors[:max_neighbors]):
            link_key = frozenset([node['id'], n['other']['id']])
            selected_links.add((link_key, n['pair']))
    
    # Створюємо фінальний список лінків
    for link_key, p in selected_links:
        links.append({
            'source': p['n1']['id'],
            'target': p['n2']['id'],
            'distance_m': round(p['dist'], 1),
            'has_los': p['has_los'],
            'terrain_los': p['terrain_los'],
            'fresnel_los': p['fresnel_los'],
            'beyond_horizon': p['beyond_horizon']
        })
    
    # Визначення майстер-нод (спрощено: хто має більше зв'язків)
    masters = []
    if len(mesh_nodes) > 1:
        conn_counts = {n['id']: 0 for n in mesh_nodes}
        for l in links:
            if l['has_los']:
                conn_counts[l['source']] += 1
                conn_counts[l['target']] += 1
        
        for nid, count in conn_counts.items():
            if count >= 2:  # Якщо вузол має мінімум 2 LOS-з'язки
                masters.append(nid)

    return jsonify({'links': links, 'masters': masters})

@application.route('/api/home', methods=['GET', 'POST', 'DELETE'])
def api_home_point():
    global home_point
    if request.method == 'GET':
        return jsonify(home_point or {})
    elif request.method == 'DELETE':
        home_point = None
        return jsonify({'ok': True})
    home_point = request.get_json()
    return jsonify({'ok': True})

@application.route('/api/clear', methods=['POST'])
def api_clear():
    global nodes, targets
    nodes.clear()
    targets.clear()
    return jsonify({'ok': True})

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    application.run(host='0.0.0.0', port=port)