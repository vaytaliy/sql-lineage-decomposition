"""Unit tests for the lineage diagram rendering module."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from PIL import Image

from diagrammer import (
    build_diagram,
    build_diagram_from_file,
    compute_layout,
    parse_lineage_csv,
)

CSV_HEADER = (
    "attribute_value,container_name,container_type,function_str,"
    "next_attribute_value,next_container_name\n"
)

SIMPLE_CSV = (
    CSV_HEADER
    + "customer_id,10__ROOT__orders,ROOT,,customer_id,7__STATEMENT__#none#\n"
    + "customer_id,7__STATEMENT__#none#,STATEMENT,,,\n"
)

TWO_COLUMN_CSV = (
    CSV_HEADER
    + "a,1__ROOT__t1,ROOT,,a,2__STATEMENT__s\n"
    + "b,3__ROOT__t2,ROOT,,b,2__STATEMENT__s\n"
    + "a,2__STATEMENT__s,STATEMENT,,,\n"
    + "b,2__STATEMENT__s,STATEMENT,,,\n"
)

EMPTY_FIELD_CSV = (
    CSV_HEADER
    + "customer_id,10__ROOT__orders,ROOT,,\n"
)


class TestParseLineageCsv(unittest.TestCase):
    """Tests for parse_lineage_csv."""

    def test_parses_containers_in_first_seen_order(self) -> None:
        """Containers appear keyed in first-seen order."""
        # Arrange / Act
        data = parse_lineage_csv(SIMPLE_CSV)

        # Assert
        self.assertEqual(
            list(data.containers),
            ["10__ROOT__orders", "7__STATEMENT__#none#"],
        )

    def test_parses_edges_with_typed_fields(self) -> None:
        """Edge fields map to the typed Edge dataclass."""
        # Arrange / Act
        data = parse_lineage_csv(SIMPLE_CSV)

        # Assert
        self.assertEqual(len(data.edges), 1)
        edge = data.edges[0]
        self.assertEqual(edge.src_cont, "10__ROOT__orders")
        self.assertEqual(edge.src_attr, "customer_id")
        self.assertEqual(edge.dst_cont, "7__STATEMENT__#none#")
        self.assertEqual(edge.dst_attr, "customer_id")

    def test_preserves_container_types(self) -> None:
        """Container types are kept from the CSV rows."""
        # Arrange / Act
        data = parse_lineage_csv(SIMPLE_CSV)

        # Assert
        self.assertEqual(data.containers["10__ROOT__orders"].type, "ROOT")

    def test_handles_empty_trailing_fields_without_crash(self) -> None:
        """Rows with missing trailing fields parse safely."""
        # Arrange / Act
        data = parse_lineage_csv(EMPTY_FIELD_CSV)

        # Assert
        self.assertEqual(len(data.edges), 0)
        self.assertIn("10__ROOT__orders", data.containers)

    def test_raises_on_empty_input(self) -> None:
        """Empty CSV input raises ValueError."""
        # Arrange / Act / Assert
        with self.assertRaises(ValueError):
            parse_lineage_csv("")

    def test_builds_container_graph(self) -> None:
        """Container adjacency graph is built from edges."""
        # Arrange / Act
        data = parse_lineage_csv(TWO_COLUMN_CSV)

        # Assert
        self.assertEqual(data.cont_graph["1__ROOT__t1"], ["2__STATEMENT__s"])
        self.assertEqual(data.cont_graph["3__ROOT__t2"], ["2__STATEMENT__s"])


class TestComputeLayout(unittest.TestCase):
    """Tests for compute_layout."""

    def test_places_connected_containers_in_separate_columns(self) -> None:
        """Connected containers land in distinct columns."""
        # Arrange
        data = parse_lineage_csv(SIMPLE_CSV)

        # Act
        layout = compute_layout(data)

        # Assert
        src_box = layout.container_boxes["10__ROOT__orders"]
        dst_box = layout.container_boxes["7__STATEMENT__#none#"]
        self.assertLess(src_box.x, dst_box.x)

    def test_anchors_exist_for_every_attribute(self) -> None:
        """Every attribute receives left/right anchors."""
        # Arrange
        data = parse_lineage_csv(TWO_COLUMN_CSV)

        # Act
        layout = compute_layout(data)

        # Assert
        for cont_name, container in data.containers.items():
            for attr in container.attrs:
                self.assertIn((cont_name, attr), layout.attr_anchors)

    def test_canvas_covers_all_containers(self) -> None:
        """Canvas dimensions cover every container box."""
        # Arrange
        data = parse_lineage_csv(TWO_COLUMN_CSV)

        # Act
        layout = compute_layout(data)

        # Assert
        for box in layout.container_boxes.values():
            self.assertLessEqual(box.x + box.width, layout.width)
            self.assertLessEqual(box.y + box.height, layout.height)


class TestBuildDiagram(unittest.TestCase):
    """Tests for the in-memory build_diagram API."""

    def test_returns_image_with_positive_dimensions(self) -> None:
        """build_diagram returns a non-empty PIL image."""
        # Arrange / Act
        img = build_diagram(SIMPLE_CSV)

        # Assert
        self.assertIsInstance(img, Image.Image)
        self.assertGreater(img.width, 0)
        self.assertGreater(img.height, 0)


class TestBuildDiagramFromFile(unittest.TestCase):
    """Tests for the file-based wrapper."""

    def test_round_trip_file_to_png(self) -> None:
        """CSV file input produces a PNG file on disk."""
        # Arrange
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / "in.csv"
            csv_path.write_text(SIMPLE_CSV, encoding="utf-8")
            out_path = Path(tmp) / "diagram"

            # Act
            result = build_diagram_from_file(csv_path, out_path)

            # Assert
            self.assertEqual(result.suffix, ".png")
            self.assertTrue(result.is_file())
            with Image.open(result) as img:
                self.assertGreater(img.width, 0)

    def test_raises_for_missing_input_file(self) -> None:
        """Missing input file raises FileNotFoundError."""
        # Arrange / Act / Assert
        with self.assertRaises(FileNotFoundError):
            build_diagram_from_file("does_not_exist.csv", "out.png")


if __name__ == "__main__":
    unittest.main()
