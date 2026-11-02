"""Shared page registration for layout-profile regions.

The OMR fiducial method preserves the existing printed-marker workflow. Other
fixed layouts can register against a reference page with ORB + RANSAC. Region
handlers consume the one aligned image returned here and must not realign it.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import cv2
import numpy as np


@dataclass(frozen=True)
class PageAlignmentResult:
    status: str
    image: np.ndarray | None
    diagnostics: dict[str, Any]


def _as_bgr_image(value: bytes | bytearray | memoryview | np.ndarray) -> np.ndarray | None:
    if isinstance(value, np.ndarray):
        if value.ndim == 2:
            return cv2.cvtColor(value, cv2.COLOR_GRAY2BGR)
        if value.ndim == 3 and value.shape[2] == 3:
            return value.copy()
        return None
    encoded = np.frombuffer(bytes(value), dtype=np.uint8)
    return cv2.imdecode(encoded, cv2.IMREAD_COLOR)


def _find_anchor_centers(image: np.ndarray) -> tuple[np.ndarray | None, dict[str, Any]]:
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    _, binary = cv2.threshold(gray, 90, 255, cv2.THRESH_BINARY_INV)
    contours, _ = cv2.findContours(binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    image_area = float(image.shape[0] * image.shape[1])
    candidates: list[tuple[float, np.ndarray, float]] = []
    for contour in contours:
        area = float(cv2.contourArea(contour))
        if area < image_area * 0.00045 or area > image_area * 0.03:
            continue
        perimeter = cv2.arcLength(contour, True)
        if perimeter <= 0:
            continue
        approx = cv2.approxPolyDP(contour, 0.035 * perimeter, True)
        if len(approx) != 4 or not cv2.isContourConvex(approx):
            continue
        x, y, w, h = cv2.boundingRect(approx)
        ratio = w / max(h, 1)
        if not 0.72 <= ratio <= 1.38 or min(w, h) < min(image.shape[:2]) * 0.018:
            continue
        moments = cv2.moments(contour)
        if moments["m00"] == 0:
            continue
        center = np.array([moments["m10"] / moments["m00"], moments["m01"] / moments["m00"]], dtype=np.float32)
        candidates.append((area, center, area / image_area))

    if len(candidates) < 4:
        return None, {"anchor_candidates": len(candidates), "alignment": "missing_fiducials"}

    candidates.sort(key=lambda item: item[0], reverse=True)
    selected = candidates[:8]
    h, w = image.shape[:2]
    assignments: list[np.ndarray] = []
    for target_x, target_y in ((0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)):
        pool = []
        for _, point, _ in selected:
            nx, ny = point[0] / w, point[1] / h
            if (target_x == 0 and nx > 0.48) or (target_x == 1 and nx < 0.52):
                continue
            if (target_y == 0 and ny > 0.48) or (target_y == 1 and ny < 0.52):
                continue
            pool.append(point)
        if not pool:
            return None, {"anchor_candidates": len(candidates), "alignment": "corner_assignment_failed"}
        assignments.append(min(pool, key=lambda p: (p[0] / w - target_x) ** 2 + (p[1] / h - target_y) ** 2))

    quad = np.array(assignments, dtype=np.float32)
    coverage = abs(cv2.contourArea(quad.reshape((-1, 1, 2)))) / image_area
    side_lengths = [float(np.linalg.norm(quad[(i + 1) % 4] - quad[i])) for i in range(4)]
    if not 0.18 <= coverage <= 0.97:
        return None, {"anchor_candidates": len(candidates), "alignment": "page_coverage_out_of_range", "coverage": round(coverage, 4)}
    if min(side_lengths) <= 0 or max(side_lengths) / min(side_lengths) > 3.5:
        return None, {"anchor_candidates": len(candidates), "alignment": "unstable_quad", "coverage": round(coverage, 4)}
    areas = sorted(item[2] for item in selected[:4])
    if areas[0] <= 0 or areas[-1] / areas[0] > 5.0:
        return None, {"anchor_candidates": len(candidates), "alignment": "marker_scale_mismatch", "coverage": round(coverage, 4)}
    return quad, {
        "anchor_candidates": len(candidates),
        "alignment": "anchors_found",
        "coverage": round(coverage, 4),
        "side_ratio": round(max(side_lengths) / min(side_lengths), 3),
    }


def _align_fiducials(image: np.ndarray, config: dict[str, Any]) -> PageAlignmentResult:
    source, diagnostics = _find_anchor_centers(image)
    if source is None:
        return PageAlignmentResult("failed", None, diagnostics)
    width, height = int(config["page_width"]), int(config["page_height"])
    target = np.array([[x * width, y * height] for x, y in config["anchors"]], dtype=np.float32)
    matrix, _ = cv2.findHomography(source, target, 0)
    if matrix is None or not np.isfinite(matrix).all() or abs(float(np.linalg.det(matrix))) < 1e-10:
        return PageAlignmentResult("failed", None, {**diagnostics, "alignment": "singular_transform"})
    warped = cv2.warpPerspective(
        image, matrix, (width, height), flags=cv2.INTER_CUBIC,
        borderMode=cv2.BORDER_CONSTANT, borderValue=(255, 255, 255),
    )
    projected = cv2.perspectiveTransform(source.reshape(1, -1, 2), matrix).reshape(-1, 2)
    errors = np.linalg.norm(projected - target, axis=1) / max(width, height)
    max_error = float(errors.max())
    if max_error > 0.002:
        return PageAlignmentResult("failed", None, {**diagnostics, "alignment": "anchor_reprojection_failed", "max_anchor_error": round(max_error, 5)})
    return PageAlignmentResult("aligned", warped, {**diagnostics, "alignment": "aligned", "method": "fiducial_anchors", "max_anchor_error": round(max_error, 5)})


def _read_reference(value: bytes | bytearray | memoryview | np.ndarray | str | Path | None) -> np.ndarray | None:
    if isinstance(value, (str, Path)):
        path = Path(value)
        if not path.is_file():
            return None
        # cv2.imread may reject Unicode paths on Windows. Read bytes through
        # pathlib, then decode from memory so portable Chinese paths work too.
        try:
            encoded = np.frombuffer(path.read_bytes(), dtype=np.uint8)
        except OSError:
            return None
        return cv2.imdecode(encoded, cv2.IMREAD_COLOR)
    if value is None:
        return None
    return _as_bgr_image(value)


def _align_orb(
    image: np.ndarray,
    reference: np.ndarray | None,
    config: dict[str, Any],
) -> PageAlignmentResult:
    if reference is None:
        return PageAlignmentResult("failed", None, {"alignment": "reference_image_missing"})
    target_h, target_w = reference.shape[:2]
    # Detect at the reference's scale; full-resolution phone noise otherwise
    # consumes the feature budget. Keep the final warp on the original pixels.
    source_h, source_w = image.shape[:2]
    scale = min(1.0, min(target_h, target_w) / min(source_h, source_w))
    detection = image
    if scale < 1.0:
        detection = cv2.resize(image, (round(source_w * scale), round(source_h * scale)), interpolation=cv2.INTER_AREA)
    source_gray = cv2.cvtColor(detection, cv2.COLOR_BGR2GRAY)
    target_gray = cv2.cvtColor(reference, cv2.COLOR_BGR2GRAY)
    orb = cv2.ORB_create(nfeatures=int(config.get("max_features", 4000)))
    source_keypoints, source_desc = orb.detectAndCompute(source_gray, None)
    target_keypoints, target_desc = orb.detectAndCompute(target_gray, None)
    if source_desc is None or target_desc is None:
        return PageAlignmentResult("failed", None, {"alignment": "features_missing", "source_keypoints": len(source_keypoints or []), "reference_keypoints": len(target_keypoints or [])})

    pairs = cv2.BFMatcher(cv2.NORM_HAMMING).knnMatch(source_desc, target_desc, k=2)
    ratio = float(config.get("ratio_test", 0.76))
    good = [first for pair in pairs if len(pair) == 2 for first, second in [pair] if first.distance < ratio * second.distance]
    min_matches = int(config.get("min_matches", 12))
    if len(good) < min_matches:
        return PageAlignmentResult("failed", None, {"alignment": "insufficient_matches", "source_keypoints": len(source_keypoints), "reference_keypoints": len(target_keypoints), "good_matches": len(good)})

    source_points = np.float32([source_keypoints[item.queryIdx].pt for item in good]).reshape(-1, 1, 2)
    source_points[:, :, 0] *= source_w / detection.shape[1]
    source_points[:, :, 1] *= source_h / detection.shape[0]
    target_points = np.float32([target_keypoints[item.trainIdx].pt for item in good]).reshape(-1, 1, 2)
    threshold_px = float(config.get("ransac_reprojection_threshold_px", 5.0))
    matrix, mask = cv2.findHomography(source_points, target_points, cv2.RANSAC, threshold_px)
    if matrix is None or mask is None or not np.isfinite(matrix).all() or abs(float(np.linalg.det(matrix))) < 1e-10:
        return PageAlignmentResult("failed", None, {"alignment": "homography_failed", "good_matches": len(good)})

    inliers = mask.ravel().astype(bool)
    inlier_count = int(inliers.sum())
    inlier_ratio = inlier_count / max(len(good), 1)
    inlier_target = target_points.reshape(-1, 2)[inliers]
    coverage = float(cv2.contourArea(cv2.convexHull(inlier_target))) / float(target_w * target_h) if inlier_count >= 3 else 0.0
    projected = cv2.perspectiveTransform(source_points, matrix).reshape(-1, 2)
    residuals = np.linalg.norm(projected - target_points.reshape(-1, 2), axis=1)[inliers]
    diagonal = float(np.hypot(target_w, target_h))
    median_error_norm = float(np.median(residuals)) / max(diagonal, 1.0) if residuals.size else 1.0
    p95_error_norm = float(np.percentile(residuals, 95)) / max(diagonal, 1.0) if residuals.size else 1.0
    details = {
        "alignment": "feature_quality_gate",
        "method": "orb_ransac",
        "detection_width": int(detection.shape[1]),
        "detection_height": int(detection.shape[0]),
        "source_keypoints": len(source_keypoints),
        "reference_keypoints": len(target_keypoints),
        "good_matches": len(good),
        "inlier_count": inlier_count,
        "inlier_ratio": round(inlier_ratio, 4),
        "inlier_page_coverage": round(coverage, 4),
        "median_reprojection_error_norm": round(median_error_norm, 6),
        "p95_reprojection_error_norm": round(p95_error_norm, 6),
    }
    if (
        inlier_count < int(config.get("min_inliers", 8))
        or inlier_ratio < float(config.get("min_inlier_ratio", 0.30))
        or p95_error_norm > float(config.get("max_reprojection_error_norm", 0.006))
    ):
        return PageAlignmentResult("failed", None, details)
    if coverage < float(config.get("min_reference_coverage", 0.04)):
        # Header/text matches can be strong but spatially concentrated. The
        # printed frame supplies the missing geometric extent; it must pass its
        # own page geometry and ink checks, never a weakened ORB acceptance.
        assisted = _align_printed_frame(image, detection, reference, details)
        if assisted is not None:
            return assisted
        return PageAlignmentResult("failed", None, details)

    warped = cv2.warpPerspective(
        image, matrix, (target_w, target_h), flags=cv2.INTER_CUBIC,
        borderMode=cv2.BORDER_CONSTANT, borderValue=(255, 255, 255),
    )
    # A header/table can match while the answer area is extrapolated far off
    # the page. Check the inverse page geometry and printed content throughout
    # the page before returning any image to the crop/signing pipeline.
    corners = np.float32([[[0, 0], [target_w, 0], [target_w, target_h], [0, target_h]]])
    projected_corners = cv2.perspectiveTransform(corners, np.linalg.inv(matrix))[0]
    normalized = projected_corners / np.float32([source_w, source_h])
    area = abs(float(cv2.contourArea(normalized)))
    geometry_ok = (
        np.isfinite(normalized).all()
        and cv2.isContourConvex(projected_corners)
        and 0.15 <= area <= 1.3
        and bool(((normalized >= -0.15) & (normalized <= 1.15)).all())
    )
    layout = _printed_layout_evidence(warped, reference)
    details.update(layout)
    details["projected_page_area_ratio"] = round(area, 4)
    if not geometry_ok or not layout["printed_layout_passed"]:
        assisted = _align_printed_frame(image, detection, reference, details)
        if assisted is not None:
            return assisted
        return PageAlignmentResult("failed", None, {**details, "alignment": "page_layout_mismatch"})
    return PageAlignmentResult("aligned", warped, {**details, "alignment": "aligned"})


def _large_printed_frame(image: np.ndarray) -> np.ndarray | None:
    """Find one unambiguous large printed box, excluding the paper outline."""
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    binary = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY_INV, 51, 8)
    binary = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
    contours, _ = cv2.findContours(binary, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    height, width = gray.shape
    frames: list[np.ndarray] = []
    for contour in sorted(contours, key=cv2.contourArea, reverse=True):
        area = cv2.contourArea(contour) / (height * width)
        if not 0.15 <= area <= 0.75:
            continue
        polygon = cv2.approxPolyDP(contour, 0.015 * cv2.arcLength(contour, True), True)
        if len(polygon) != 4 or not cv2.isContourConvex(polygon):
            continue
        points = polygon.reshape(4, 2).astype(np.float32)
        # Stable clockwise corner order, independent of contour direction.
        points = points[np.argsort(np.arctan2(points[:, 1] - points[:, 1].mean(), points[:, 0] - points[:, 0].mean()))]
        points = np.roll(points, -int(np.argmin(points.sum(axis=1))), axis=0)
        if any(float(np.max(np.linalg.norm(points - old, axis=1))) < min(height, width) * 0.015 for old in frames):
            continue  # Inner/outer edge of the same printed border.
        frames.append(points)
    return frames[0] if len(frames) == 1 else None


def _align_printed_frame(image: np.ndarray, detection: np.ndarray, reference: np.ndarray, feature_support: dict[str, Any]) -> PageAlignmentResult | None:
    """Teacher-assisted alignment for edited answer cards with a shared frame.

    Requires sufficient feature count, inlier ratio and reprojection accuracy.
    The feature coverage may be low; a frame alone cannot select an exam page:
    page-wide printed evidence must also agree.
    """
    source_frame = _large_printed_frame(detection)
    target_frame = _large_printed_frame(reference)
    if source_frame is None or target_frame is None:
        return None
    source_frame *= np.float32([image.shape[1] / detection.shape[1], image.shape[0] / detection.shape[0]])
    matrix = cv2.getPerspectiveTransform(source_frame, target_frame)
    if not np.isfinite(matrix).all() or abs(float(np.linalg.det(matrix))) < 1e-10:
        return None
    height, width = reference.shape[:2]
    corners = cv2.perspectiveTransform(np.float32([[[0, 0], [width, 0], [width, height], [0, height]]]), np.linalg.inv(matrix))[0]
    normalized = corners / np.float32([image.shape[1], image.shape[0]])
    area = abs(float(cv2.contourArea(normalized)))
    if (not np.isfinite(normalized).all() or not cv2.isContourConvex(corners)
            or not 0.15 <= area <= 1.3 or not ((normalized >= -0.15) & (normalized <= 1.15)).all()):
        return None
    warped = cv2.warpPerspective(image, matrix, (width, height), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_CONSTANT, borderValue=(255, 255, 255))
    evidence = _printed_layout_evidence(warped, reference)
    if not evidence["printed_layout_passed"]:
        return None
    frame_errors = np.linalg.norm(
        cv2.perspectiveTransform(source_frame.reshape(1, 4, 2), matrix)[0] - target_frame, axis=1,
    ) / float(np.hypot(height, width))
    return PageAlignmentResult("aligned", warped, {
        "alignment": "aligned", "method": "printed_frame_teacher_assisted",
        "alignment_assisted": True,
        "alignment_evidence": "four_printed_frame_corners_and_page_ink",
        "feature_support": feature_support,
        "good_matches": 4, "inlier_count": 4, "inlier_ratio": 1.0,
        "inlier_page_coverage": round(abs(float(cv2.contourArea(target_frame))) / (width * height), 4),
        "median_reprojection_error_norm": round(float(np.median(frame_errors)), 6),
        "p95_reprojection_error_norm": round(float(np.percentile(frame_errors, 95)), 6),
        "projected_page_area_ratio": round(area, 4), **evidence,
    })


def _printed_layout_evidence(warped: np.ndarray, reference: np.ndarray) -> dict[str, Any]:
    """Check reference ink recall by vertical band, tolerating extra handwriting.

    This is an additional rejection gate, never an alternative registration or
    permission to send a full page. Blank bands supply no positive evidence.
    """
    gray = cv2.cvtColor(warped, cv2.COLOR_BGR2GRAY)
    ref_gray = cv2.cvtColor(reference, cv2.COLOR_BGR2GRAY)
    photo_white = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 31, 12)
    distance = cv2.distanceTransform(photo_white, cv2.DIST_L2, 3)
    ink = ref_gray < 160
    height, width = gray.shape
    tolerance = max(2.0, float(np.hypot(height, width)) * 0.002)
    scores = []
    for band in range(3):
        section = slice(round(height * band / 3), round(height * (band + 1) / 3))
        selected = ink[section]
        count = int(selected.sum())
        if count >= 64:
            scores.append(float(np.mean(distance[section][selected] <= tolerance)))
    return {
        "printed_layout_band_recall": [round(score, 4) for score in scores],
        "printed_layout_passed": len(scores) >= 2 and min(scores) >= 0.35,
    }


def align_page(
    image: bytes | bytearray | memoryview | np.ndarray,
    alignment_config: dict[str, Any],
    *,
    reference_image: bytes | bytearray | memoryview | np.ndarray | str | Path | None = None,
) -> PageAlignmentResult:
    """Register a photographed page once into the profile's template space."""
    source = _as_bgr_image(image)
    if source is None:
        return PageAlignmentResult("failed", None, {"alignment": "decode_failed"})
    method = str(alignment_config.get("method", "orb_ransac"))
    if method == "fiducial_anchors":
        template = alignment_config.get("template_config", alignment_config)
        try:
            return _align_fiducials(source, template)
        except (KeyError, TypeError, ValueError, cv2.error) as exc:
            return PageAlignmentResult("failed", None, {"alignment": "invalid_fiducial_config", "error_type": type(exc).__name__})
    if method == "orb_ransac":
        return _align_orb(source, _read_reference(reference_image), alignment_config)
    return PageAlignmentResult("failed", None, {"alignment": "unsupported_method"})
