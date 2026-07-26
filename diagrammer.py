"""Diagram rendering for SQL column-lineage CSV data.

The module is organized in layers so that each stage can be used independently:

1. Parsing   -- :func:`parse_lineage_csv` turns CSV text into a typed
   :class:`DiagramData` model.
2. Layout    -- :func:`compute_layout` assigns pixel positions to containers
   and attributes.
3. Routing   -- :func:`route_edges` computes collision-free edge skeletons.
4. Rendering -- :func:`render_diagram` draws the model onto a PIL image.

Use :func:`build_diagram` to obtain an in-memory ``Image`` from a CSV string,
or :func:`build_diagram_from_file` for file-to-file generation (used by the CLI).
"""

from __future__ import annotations

import argparse
import csv
import io
import math
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from PIL import Image, ImageDraw, ImageFont

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PATH_COLORS = [
    '#E53935', '#1E88E5', '#43A047', '#FDD835',
    '#8E24AA', '#F4511E', '#3949AB', '#00ACC1'
]

CONTAINER_TYPE_COLORS = {
    'ROOT': '#E8F5E9',
    'STATEMENT': '#E3F2FD',
    'SUBQUERY': '#FFF3E0',
}
DEFAULT_CONTAINER_COLOR = '#F5F5F5'
DEFAULT_CONTAINER_TYPE = 'UNKNOWN'

BACKGROUND_COLOR = '#FFFFFF'
CONTAINER_OUTLINE_COLOR = '#78909C'
HEADER_BACKGROUND_COLOR = '#CFD8DC'
TITLE_TEXT_COLOR = '#000000'
ATTRIBUTE_TEXT_COLOR = '#263238'

CONTAINER_WIDTH = 260
ATTRIBUTE_HEIGHT = 28
HEADER_HEIGHT = 38
MARGIN_X = 250
MARGIN_Y = 90
CONTAINER_BOTTOM_PADDING = 10
ATTRIBUTE_ANCHOR_Y_OFFSET = 14
ATTRIBUTE_TEXT_X_OFFSET = 16
ATTRIBUTE_TEXT_Y_OFFSET = 7
TITLE_TEXT_X_OFFSET = 12
TITLE_TEXT_Y_OFFSET = 12

TRACK_COLLISION_BUFFER = 6
TRACK_SEARCH_STEP = 8
OBSTACLE_DETOUR_PADDING = 20
DETOUR_Y_OFFSET = 15
ARCH_RADIUS = 5
ARCH_STEPS = 6
ORIGIN_CORRIDOR_LENGTH = 150
DESTINATION_CORRIDOR_LENGTH = 40
EDGE_VERTICAL_BASE_OFFSET = 30
EDGE_VERTICAL_STRIDE = 12
EDGE_VERTICAL_VARIANTS = 15

LINE_WIDTH = 2
ARROWHEAD_LENGTH = 10
ARROWHEAD_HALF_WIDTH = 5
CORNER_RADIUS = 14
CORNER_ARC_STEPS = 4

CSV_SNIFF_SAMPLE_SIZE = 4096
FONT_SIZE = 12
WINDOWS_FONT_NAME = 'arial.ttf'
PNG_EXTENSION = '.png'

COLUMN_ATTRIBUTE_VALUE = 'attribute_value'
COLUMN_CONTAINER_NAME = 'container_name'
COLUMN_CONTAINER_TYPE = 'container_type'
COLUMN_NEXT_ATTRIBUTE_VALUE = 'next_attribute_value'
COLUMN_NEXT_CONTAINER_NAME = 'next_container_name'

Point = tuple[int, int]
Track = tuple[int, int, int]
Obstacle = tuple[int, int, int, int]
VerticalSegment = tuple[int, int, int]


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class Container:
    """A diagram box grouping a named set of attributes.

    Attributes:
        name: Unique container identifier.
        type: Container type key (ROOT, STATEMENT, SUBQUERY, ...).
        attrs: Ordered, de-duplicated attribute names.
    """

    name: str
    type: str
    attrs: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Edge:
    """A directed lineage connection between two container attributes."""

    src_cont: str
    src_attr: str
    dst_cont: str
    dst_attr: str


@dataclass
class DiagramData:
    """Parsed representation of the lineage CSV input.

    Attributes:
        containers: Containers keyed by name, in first-seen order.
        edges: All directed lineage edges.
        cont_graph: Adjacency list of container-to-container connections.
    """

    containers: dict[str, Container] = field(default_factory=dict)
    edges: list[Edge] = field(default_factory=list)
    cont_graph: dict[str, list[str]] = field(default_factory=lambda: defaultdict(list))


@dataclass(frozen=True)
class ContainerBox:
    """Pixel rectangle occupied by a container."""

    x: int
    y: int
    width: int
    height: int


@dataclass(frozen=True)
class AttrAnchors:
    """Connection points on the left and right edge of an attribute row."""

    left: Point
    right: Point


@dataclass
class Layout:
    """Computed pixel geometry for the whole diagram.

    Attributes:
        container_boxes: Container rectangles keyed by container name.
        attr_anchors: Anchor points keyed by (container name, attribute name).
        width: Total canvas width in pixels.
        height: Total canvas height in pixels.
    """

    container_boxes: dict[str, ContainerBox]
    attr_anchors: dict[tuple[str, str], AttrAnchors]
    width: int
    height: int


@dataclass(frozen=True)
class RoutedEdge:
    """An edge together with its computed waypoint skeleton and color."""

    edge: Edge
    skeleton: list[Point]
    color: str


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def _sniff_dialect(sample: str) -> type[csv.Dialect]:
    """Detect the CSV dialect, falling back to standard comma-separated excel.

    Args:
        sample: Leading text sample of the CSV content.

    Returns:
        The detected dialect, or ``csv.excel`` when detection fails.
    """
    try:
        return csv.Sniffer().sniff(sample)
    except csv.Error:
        return csv.excel


def parse_lineage_csv(csv_text: str) -> DiagramData:
    """Parse lineage CSV text into a typed diagram model.

    Args:
        csv_text: CSV content with columns attribute_value, container_name,
            container_type, and optionally next_attribute_value /
            next_container_name.

    Returns:
        The parsed diagram data.

    Raises:
        ValueError: If the CSV contains no data rows.
    """
    data = DiagramData()
    reader_stream = io.StringIO(csv_text)
    sample = csv_text[:CSV_SNIFF_SAMPLE_SIZE]
    dialect = _sniff_dialect(sample) if sample else csv.excel

    for row in csv.DictReader(reader_stream, dialect=dialect):
        src_attr = (row.get(COLUMN_ATTRIBUTE_VALUE) or '').strip()
        src_cont = (row.get(COLUMN_CONTAINER_NAME) or '').strip()
        ctype = (row.get(COLUMN_CONTAINER_TYPE) or '').strip()
        dst_attr = (row.get(COLUMN_NEXT_ATTRIBUTE_VALUE) or '').strip()
        dst_cont = (row.get(COLUMN_NEXT_CONTAINER_NAME) or '').strip()

        if not src_cont:
            continue

        if src_cont not in data.containers:
            data.containers[src_cont] = Container(name=src_cont, type=ctype)
        if src_attr and src_attr not in data.containers[src_cont].attrs:
            data.containers[src_cont].attrs.append(src_attr)

        if dst_attr and dst_cont:
            if dst_cont not in data.containers:
                data.containers[dst_cont] = Container(name=dst_cont, type=DEFAULT_CONTAINER_TYPE)
            if dst_attr not in data.containers[dst_cont].attrs:
                data.containers[dst_cont].attrs.append(dst_attr)
            data.edges.append(Edge(src_cont=src_cont, src_attr=src_attr,
                                   dst_cont=dst_cont, dst_attr=dst_attr))
            data.cont_graph[src_cont].append(dst_cont)

    if not data.containers:
        raise ValueError('CSV input contains no lineage rows.')
    return data


# ---------------------------------------------------------------------------
# Layout
# ---------------------------------------------------------------------------

def _compute_depths(data: DiagramData) -> dict[str, int]:
    """Assign a column depth to each container via longest-path relaxation.

    Args:
        data: Parsed diagram data.

    Returns:
        Mapping of container name to zero-based column depth.
    """
    depths = {name: 0 for name in data.containers}
    for _ in range(len(data.containers)):
        for src, dsts in data.cont_graph.items():
            for dst in dsts:
                depths[dst] = max(depths[dst], depths[src] + 1)
    return depths


def _position_column(data: DiagramData, col_idx: int, containers: list[str],
                     container_boxes: dict[str, ContainerBox],
                     attr_anchors: dict[tuple[str, str], AttrAnchors]) -> tuple[int, int]:
    """Position all containers of a single depth column.

    Args:
        data: Parsed diagram data.
        col_idx: Zero-based depth column index.
        containers: Container names belonging to this column.
        container_boxes: Mutable output registry of container rectangles.
        attr_anchors: Mutable output registry of attribute anchor points.

    Returns:
        The (max_x, max_y) pixel extent reached by this column.
    """
    max_x, max_y = 0, 0
    current_y = MARGIN_Y
    x = MARGIN_X + col_idx * (CONTAINER_WIDTH + MARGIN_X)
    for cont in containers:
        attrs = data.containers[cont].attrs
        height = HEADER_HEIGHT + len(attrs) * ATTRIBUTE_HEIGHT + CONTAINER_BOTTOM_PADDING
        container_boxes[cont] = ContainerBox(x=x, y=current_y,
                                             width=CONTAINER_WIDTH, height=height)
        for i, attr in enumerate(attrs):
            anchor_y = (current_y + HEADER_HEIGHT + i * ATTRIBUTE_HEIGHT
                        + ATTRIBUTE_ANCHOR_Y_OFFSET)
            attr_anchors[(cont, attr)] = AttrAnchors(left=(x, anchor_y),
                                                     right=(x + CONTAINER_WIDTH, anchor_y))
        current_y += height + MARGIN_Y
        max_y = max(max_y, current_y)
        max_x = max(max_x, x + CONTAINER_WIDTH + MARGIN_X)
    return max_x, max_y


def compute_layout(data: DiagramData) -> Layout:
    """Compute pixel positions for all containers and attribute anchors.

    Args:
        data: Parsed diagram data.

    Returns:
        The computed layout geometry.
    """
    depths = _compute_depths(data)
    columns: dict[int, list[str]] = defaultdict(list)
    for name, depth in depths.items():
        columns[depth].append(name)

    container_boxes: dict[str, ContainerBox] = {}
    attr_anchors: dict[tuple[str, str], AttrAnchors] = {}
    max_x, max_y = 0, 0

    for col_idx in sorted(columns):
        col_max_x, col_max_y = _position_column(data, col_idx, columns[col_idx],
                                                container_boxes, attr_anchors)
        max_x, max_y = max(max_x, col_max_x), max(max_y, col_max_y)

    return Layout(container_boxes=container_boxes, attr_anchors=attr_anchors,
                  width=max_x, height=max_y)


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------

def is_y_safe(y: int, x_start: int, x_end: int, tracks: list[Track],
              buffer: int = TRACK_COLLISION_BUFFER) -> bool:
    """Check whether a horizontal path segment collides with registered tracks.

    Args:
        y: Y coordinate of the candidate segment.
        x_start: Left X bound of the segment.
        x_end: Right X bound of the segment.
        tracks: Registered (y, x1, x2) horizontal tracks.
        buffer: Minimum vertical clearance between tracks.

    Returns:
        True when the segment does not overlap any registered track.
    """
    for ty, tx1, tx2 in tracks:
        if abs(y - ty) <= buffer and max(x_start, tx1) < min(x_end, tx2):
            return False
    return True


def allocate_safe_y(desired_y: int, x_start: int, x_end: int,
                    tracks: list[Track]) -> int:
    """Find the nearest unoccupied horizontal track and register it.

    Args:
        desired_y: Preferred Y coordinate.
        x_start: Left X bound of the segment.
        x_end: Right X bound of the segment.
        tracks: Mutable registry of occupied tracks.

    Returns:
        The allocated collision-free Y coordinate.
    """
    offset = 0
    while True:
        if is_y_safe(desired_y + offset, x_start, x_end, tracks):
            tracks.append((desired_y + offset, x_start, x_end))
            return desired_y + offset
        if offset != 0 and is_y_safe(desired_y - offset, x_start, x_end, tracks):
            tracks.append((desired_y - offset, x_start, x_end))
            return desired_y - offset
        offset += TRACK_SEARCH_STEP


def clean_skeleton(skel: list[Point]) -> list[Point]:
    """Remove redundant points and merge collinear segments.

    Args:
        skel: Raw waypoint list.

    Returns:
        A minimized waypoint list describing the same polyline.
    """
    if not skel:
        return []
    cleaned = [skel[0]]
    for p in skel[1:]:
        if p != cleaned[-1]:
            if len(cleaned) >= 2:
                p_prev, p_prev2 = cleaned[-1], cleaned[-2]
                if (p_prev2[0] == p_prev[0] == p[0]) or (p_prev2[1] == p_prev[1] == p[1]):
                    cleaned[-1] = p
                    continue
            cleaned.append(p)
    return cleaned


def route_skeleton(x_start: int, x_end: int, y: int, obstacles: list[Obstacle],
                   tracks: list[Track]) -> list[Point]:
    """Generate waypoints for a horizontal route, detouring under obstacles.

    Args:
        x_start: Left X bound of the route.
        x_end: Right X bound of the route.
        y: Y coordinate of the route.
        obstacles: Container rectangles as (x1, y1, x2, y2).
        tracks: Mutable registry of occupied horizontal tracks.

    Returns:
        Waypoints describing the collision-free route.
    """
    points = [(x_start, y)]
    current_x = x_start

    intersecting = [obs for obs in obstacles
                    if obs[0] < x_end and obs[2] > current_x and obs[1] <= y <= obs[3]]
    intersecting.sort(key=lambda o: o[0])

    for obs in intersecting:
        ox1, _, ox2, oy2 = obs
        pre_obs_x, post_obs_x = ox1 - OBSTACLE_DETOUR_PADDING, ox2 + OBSTACLE_DETOUR_PADDING

        if current_x < pre_obs_x:
            points.append((pre_obs_x, y))

        detour_y = allocate_safe_y(oy2 + DETOUR_Y_OFFSET, pre_obs_x, post_obs_x, tracks)

        points.extend([(pre_obs_x, detour_y), (post_obs_x, detour_y), (post_obs_x, y)])
        current_x = post_obs_x

    points.append((x_end, y))
    return points


def build_arched_segment(x_start: int, x_end: int, y: int,
                         v_segments: list[VerticalSegment],
                         r_jump: int = ARCH_RADIUS) -> list[Point]:
    """Translate waypoints into a drawn path, inserting arches at crossings.

    Args:
        x_start: Left X bound of the segment.
        x_end: Right X bound of the segment.
        y: Y coordinate of the segment.
        v_segments: Registered vertical drops as (x, y_min, y_max).
        r_jump: Radius of the jump arch.

    Returns:
        Dense point list with semicircular arches over each crossing.
    """
    path = []
    if x_start == x_end:
        return [(x_start, y)]

    intersections = [vx for vx, v_min, v_max in v_segments
                     if x_start < vx < x_end and v_min < y < v_max]
    intersections.sort()

    current_x = x_start
    for vx in intersections:
        if current_x < vx - r_jump:
            path.extend([(current_x, y), (vx - r_jump, y)])
        for i in range(ARCH_STEPS + 1):
            angle = math.pi - (i * math.pi / ARCH_STEPS)
            path.append((int(vx + r_jump * math.cos(angle)),
                         int(y - r_jump * math.sin(angle))))
        current_x = vx + r_jump

    path.extend([(current_x, y), (x_end, y)])
    return path


def _assign_edge_colors(edges: list[Edge]) -> dict[str, str]:
    """Assign a stable color per source container, in first-seen order.

    Args:
        edges: All diagram edges.

    Returns:
        Mapping of source container name to hex color.
    """
    unique_sources = list(dict.fromkeys(e.src_cont for e in edges))
    return {src: PATH_COLORS[i % len(PATH_COLORS)] for i, src in enumerate(unique_sources)}


def _register_edge_corridors(edges: list[Edge], layout: Layout,
                             tracks: list[Track]) -> None:
    """Pre-register strict origin and destination corridors.

    Blocking these corridors prevents approach paths from overlapping the
    exact tracks each edge must start and end on.

    Args:
        edges: All diagram edges.
        layout: Computed layout geometry.
        tracks: Mutable registry of occupied horizontal tracks.
    """
    for edge in edges:
        x1, y1 = layout.attr_anchors[(edge.src_cont, edge.src_attr)].right
        x2, y2 = layout.attr_anchors[(edge.dst_cont, edge.dst_attr)].left
        tracks.append((y1, x1, x1 + ORIGIN_CORRIDOR_LENGTH))
        tracks.append((y2, x2 - DESTINATION_CORRIDOR_LENGTH, x2))
        if y1 == y2:
            tracks.append((y1, x1, x2))


def _route_single_edge(edge: Edge, index: int, layout: Layout,
                       obstacles: list[Obstacle], tracks: list[Track]) -> list[Point]:
    """Route one edge skeleton around obstacles and occupied tracks.

    Args:
        edge: The edge to route.
        index: Edge ordinal, used to stagger vertical drop positions.
        layout: Computed layout geometry.
        obstacles: Container rectangles as (x1, y1, x2, y2).
        tracks: Mutable registry of occupied horizontal tracks.

    Returns:
        The cleaned waypoint skeleton for the edge.
    """
    x1, y1 = layout.attr_anchors[(edge.src_cont, edge.src_attr)].right
    x2, y2 = layout.attr_anchors[(edge.dst_cont, edge.dst_attr)].left

    mid_x = int(x1 + EDGE_VERTICAL_BASE_OFFSET
                + (index % EDGE_VERTICAL_VARIANTS) * EDGE_VERTICAL_STRIDE)
    skel: list[Point] = []

    if y1 == y2:
        skel.extend(route_skeleton(x1, x2, y1, obstacles, tracks))
    else:
        app_y = allocate_safe_y(y2, mid_x, x2 - OBSTACLE_DETOUR_PADDING, tracks)
        skel.extend(route_skeleton(x1, mid_x, y1, obstacles, tracks))
        skel.append((mid_x, app_y))
        skel.extend(route_skeleton(mid_x, x2 - OBSTACLE_DETOUR_PADDING,
                                   app_y, obstacles, tracks))
        skel.append((x2, y2))

    return clean_skeleton(skel)


def _collect_vertical_segments(skel: list[Point],
                               v_segments: list[VerticalSegment]) -> None:
    """Register the vertical drops of a skeleton for arch rendering.

    Args:
        skel: Waypoint skeleton of a routed edge.
        v_segments: Mutable registry of vertical segments as (x, y_min, y_max).
    """
    for j in range(len(skel) - 1):
        p1, p2 = skel[j], skel[j + 1]
        if p1[0] == p2[0] and p1[1] != p2[1]:
            v_segments.append((p1[0], min(p1[1], p2[1]), max(p1[1], p2[1])))


def route_edges(data: DiagramData,
                layout: Layout) -> tuple[list[RoutedEdge], list[VerticalSegment]]:
    """Compute collision-free waypoint skeletons for every edge.

    Uses a two-pass approach: strict origin/destination corridors are
    pre-registered so approach paths cannot overlap them, then each edge
    skeleton is routed and its vertical drops are registered for arch
    rendering.

    Args:
        data: Parsed diagram data.
        layout: Computed layout geometry.

    Returns:
        A tuple of routed edges and the registry of vertical segments.
    """
    obstacles = [(b.x, b.y, b.x + b.width, b.y + b.height)
                 for b in layout.container_boxes.values()]
    color_map = _assign_edge_colors(data.edges)

    tracks: list[Track] = []
    v_segments: list[VerticalSegment] = []
    routed: list[RoutedEdge] = []

    _register_edge_corridors(data.edges, layout, tracks)

    for i, edge in enumerate(data.edges):
        skel = _route_single_edge(edge, i, layout, obstacles, tracks)
        routed.append(RoutedEdge(edge=edge, skeleton=skel, color=color_map[edge.src_cont]))
        _collect_vertical_segments(skel, v_segments)

    return routed, v_segments


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def _load_font() -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """Load the diagram font, falling back to the PIL default when unavailable.

    Returns:
        A usable image font.
    """
    if sys.platform == 'win32':
        try:
            return ImageFont.truetype(WINDOWS_FONT_NAME, FONT_SIZE)
        except OSError:
            pass
    return ImageFont.load_default()


def _quarter_arc(corner: Point, incoming: Point, outgoing: Point,
                 radius: int) -> list[Point]:
    """Compute arc points smoothing a single 90-degree corner.

    The arc is tangent to both adjoining segments; its radius is clamped to
    half of the shorter adjoining segment so adjacent corners never overlap.

    Args:
        corner: The sharp waypoint to smooth.
        incoming: Waypoint before the corner.
        outgoing: Waypoint after the corner.
        radius: Desired corner radius in pixels.

    Returns:
        Dense points from the tangent point on the incoming segment to the
        tangent point on the outgoing segment.
    """
    len_in = abs(corner[0] - incoming[0]) + abs(corner[1] - incoming[1])
    len_out = abs(corner[0] - outgoing[0]) + abs(corner[1] - outgoing[1])
    r = min(radius, len_in // 2, len_out // 2)
    if r < 2:
        return [corner]

    dir_in = ((incoming[0] - corner[0]) // len_in,
              (incoming[1] - corner[1]) // len_in)
    dir_out = ((outgoing[0] - corner[0]) // len_out,
               (outgoing[1] - corner[1]) // len_out)

    tangent_in = (corner[0] + r * dir_in[0], corner[1] + r * dir_in[1])
    tangent_out = (corner[0] + r * dir_out[0], corner[1] + r * dir_out[1])
    center = (tangent_in[0] + r * dir_out[0], tangent_in[1] + r * dir_out[1])

    start_angle = math.atan2(tangent_in[1] - center[1], tangent_in[0] - center[0])
    end_angle = math.atan2(tangent_out[1] - center[1], tangent_out[0] - center[0])
    sweep = (end_angle - start_angle + math.pi) % (2 * math.pi) - math.pi

    return [
        (int(round(center[0] + r * math.cos(start_angle + sweep * i / CORNER_ARC_STEPS))),
         int(round(center[1] + r * math.sin(start_angle + sweep * i / CORNER_ARC_STEPS))))
        for i in range(CORNER_ARC_STEPS + 1)
    ]


def round_skeleton_corners(skel: list[Point], radius: int = CORNER_RADIUS) -> list[Point]:
    """Replace sharp 90-degree corners of a skeleton with rounded arcs.

    Straight runs remain axis-aligned, so arch detection on horizontal
    segments stays valid after rounding.

    Args:
        skel: Waypoint skeleton with axis-aligned segments.
        radius: Desired corner radius in pixels.

    Returns:
        A densified waypoint list with quarter-circle corners.
    """
    if len(skel) < 3:
        return skel
    rounded = [skel[0]]
    for i in range(1, len(skel) - 1):
        rounded.extend(_quarter_arc(skel[i], skel[i - 1], skel[i + 1], radius))
    rounded.append(skel[-1])
    return rounded


def _draw_connections(draw: ImageDraw.ImageDraw, routed: list[RoutedEdge],
                      v_segments: list[VerticalSegment]) -> None:
    """Draw routed edge polylines with arches and arrowheads.

    Args:
        draw: The PIL drawing context.
        routed: Routed edges with skeletons and colors.
        v_segments: Registered vertical drops used for arch placement.
    """
    for routed_edge in routed:
        skel = round_skeleton_corners(routed_edge.skeleton)
        final_path: list[Point] = []
        for i in range(len(skel) - 1):
            p1, p2 = skel[i], skel[i + 1]
            if p1[1] == p2[1]:
                final_path.extend(build_arched_segment(p1[0], p2[0], p1[1], v_segments))
            else:
                final_path.extend([p1, p2])

        draw.line(final_path, fill=routed_edge.color, width=LINE_WIDTH)
        target = skel[-1]
        draw.polygon([(target[0], target[1]),
                      (target[0] - ARROWHEAD_LENGTH, target[1] - ARROWHEAD_HALF_WIDTH),
                      (target[0] - ARROWHEAD_LENGTH, target[1] + ARROWHEAD_HALF_WIDTH)],
                     fill=routed_edge.color)


def _draw_containers(draw: ImageDraw.ImageDraw, data: DiagramData,
                     layout: Layout, font: ImageFont.ImageFont | ImageFont.FreeTypeFont) -> None:
    """Draw container boxes, headers and attribute labels.

    Args:
        draw: The PIL drawing context.
        data: Parsed diagram data.
        layout: Computed layout geometry.
        font: Font used for all labels.
    """
    for cont, box in layout.container_boxes.items():
        bg = CONTAINER_TYPE_COLORS.get(data.containers[cont].type, DEFAULT_CONTAINER_COLOR)
        draw.rectangle([box.x, box.y, box.x + box.width, box.y + box.height],
                       fill=bg, outline=CONTAINER_OUTLINE_COLOR, width=LINE_WIDTH)
        draw.rectangle([box.x, box.y, box.x + box.width, box.y + HEADER_HEIGHT],
                       fill=HEADER_BACKGROUND_COLOR, outline=CONTAINER_OUTLINE_COLOR,
                       width=LINE_WIDTH)
        draw.text((box.x + TITLE_TEXT_X_OFFSET, box.y + TITLE_TEXT_Y_OFFSET),
                  cont, fill=TITLE_TEXT_COLOR, font=font)
        for i, attr in enumerate(data.containers[cont].attrs):
            text_y = box.y + HEADER_HEIGHT + i * ATTRIBUTE_HEIGHT + ATTRIBUTE_TEXT_Y_OFFSET
            draw.text((box.x + ATTRIBUTE_TEXT_X_OFFSET, text_y),
                      attr, fill=ATTRIBUTE_TEXT_COLOR, font=font)


def render_diagram(data: DiagramData, layout: Layout, routed: list[RoutedEdge],
                   v_segments: list[VerticalSegment]) -> Image.Image:
    """Render the diagram model onto a new image.

    Args:
        data: Parsed diagram data.
        layout: Computed layout geometry.
        routed: Routed edges with skeletons and colors.
        v_segments: Registered vertical drops used for arch placement.

    Returns:
        The rendered diagram image.
    """
    img = Image.new('RGB', (layout.width, layout.height), BACKGROUND_COLOR)
    draw = ImageDraw.Draw(img)
    font = _load_font()
    _draw_connections(draw, routed, v_segments)
    _draw_containers(draw, data, layout, font)
    return img


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def build_diagram(csv_text: str) -> Image.Image:
    """Build a lineage diagram from CSV text and return it in memory.

    Args:
        csv_text: Lineage CSV content (see :func:`parse_lineage_csv`).

    Returns:
        The rendered diagram image.
    """
    data = parse_lineage_csv(csv_text)
    layout = compute_layout(data)
    routed, v_segments = route_edges(data, layout)
    return render_diagram(data, layout, routed, v_segments)


def save(img: Image.Image, output_filename: str) -> str:
    """Save a rendered diagram image to disk as a PNG.

    Args:
        img: The rendered diagram image.
        output_filename: Destination path; the .png extension is appended
            when missing.

    Returns:
        The path of the written PNG file.
    """
    output_path = Path(output_filename)
    if output_path.suffix.lower() != PNG_EXTENSION:
        output_path = output_path.with_suffix(PNG_EXTENSION)
    img.save(output_path)
    return str(output_path)

def build_diagram_from_file(input_csv: str | Path, output_filename: str | Path) -> Path:
    """Build a diagram from a CSV file and save it as a PNG.

    Args:
        input_csv: Path to the lineage CSV file.
        output_filename: Destination path; the .png extension is appended
            when missing.

    Returns:
        The path of the written PNG file.

    Raises:
        FileNotFoundError: If the input CSV does not exist.
        ValueError: If the CSV contains no lineage rows.
    """
    input_path = Path(input_csv)
    if not input_path.is_file():
        raise FileNotFoundError(f'Could not find {input_path}')

    img = build_diagram(input_path.read_text(encoding='utf-8'))

    output_path = Path(output_filename)
    if output_path.suffix.lower() != PNG_EXTENSION:
        output_path = output_path.with_suffix(PNG_EXTENSION)
    img.save(output_path)
    return output_path


def main(argv: Optional[list[str]] = None) -> int:
    """CLI entry point for file-to-file diagram generation.

    Args:
        argv: Optional argument list (defaults to ``sys.argv``).

    Returns:
        Process exit code: 0 on success, 1 on input errors.
    """
    parser = argparse.ArgumentParser(description='Render a lineage diagram from CSV.')
    parser.add_argument('-i', '--input', required=True, help='Input lineage CSV file.')
    parser.add_argument('-o', '--output', default='out.png', help='Output PNG file.')
    args = parser.parse_args(argv)

    try:
        out = build_diagram_from_file(args.input, args.output)
    except (FileNotFoundError, ValueError) as exc:
        print(f'Error: {exc}')
        return 1
    print(f'Diagram generated strictly without overlap at {out}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
