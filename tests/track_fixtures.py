"""Wholly synthetic closed circuit (rounded rectangle); no real track coordinates."""
import math

LENGTH = 3000.0


def circuit(distance, length=LENGTH):
    """Position on a stadium-shaped loop for a lap distance. Returns (x, z) in metres."""
    d = distance % length
    straight, radius = length * 0.3, (length * 0.2) / math.pi
    segments = [straight, math.pi * radius, straight, math.pi * radius]
    if d < segments[0]:
        return d, -radius
    d -= segments[0]
    if d < segments[1]:
        a = -math.pi / 2 + d / radius
        return straight + radius * math.cos(a), radius * math.sin(a)
    d -= segments[1]
    if d < segments[2]:
        return straight - d, radius
    d -= segments[2]
    a = math.pi / 2 + d / radius
    return radius * math.cos(a), radius * math.sin(a)


def lap_samples(start=0.0, end=LENGTH, step=12.0, pits=lambda d: False, skip=lambda d: False, length=LENGTH):
    """Samples (lap_dist, x, z, in_pits) at a constant spacing; lap_dist wraps and overshoots slightly."""
    d, out = start, []
    while d < end:
        if not skip(d):
            ld = d % length
            if ld > length - 3:
                ld = ld - length  # providers report slightly negative values right at the line
            x, z = circuit(d, length)
            out.append((ld, x, z, bool(pits(d))))
        d += step
    return out


def closed_geometry(arc=12.0, spacing=15.0, along=lambda d: 0.0, length=LENGTH):
    """(points, distances) of a closed stored geometry on the synthetic circuit with a closing arc of `arc` metres.

    The line (lap distance 0) lies in the middle of the arc. `along(d)` displaces the position that belongs to lap
    distance d by that many metres along the track, which models a position that disagrees with the lap distance
    (the provider's two values are not guaranteed to agree to the metre at the line). Entirely synthetic."""
    count = max(2, round((length - arc) / spacing))
    step = (length - arc) / count
    distances = [arc / 2 + i * step for i in range(count + 1)]
    return [list(circuit(d + along(d), length)) for d in distances], distances


def model_from(points, distances, length=LENGTH):
    """A TrackModel that holds the given closed geometry, as it would after adopting a stored record."""
    from server.app.trackmap import TrackModel
    model = TrackModel(length)
    model.points, model.distances = [list(p) for p in points], list(distances)
    return model
