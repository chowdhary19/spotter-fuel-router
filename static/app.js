/* No station or user-provided strings are inserted with innerHTML. */
const $ = (id) => document.getElementById(id);
const map = typeof L === "undefined" ? null : L.map("map").setView([39.5, -98.35], 4);
let layer;

if (map) {
  L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19,
    attribution: "© OpenStreetMap contributors",
  }).addTo(map);
}

function text(tag, content) {
  const element = document.createElement(tag);
  element.textContent = content;
  return element;
}

function clearDisplayedPlan() {
  $("summary").hidden = true;
  $("metrics").replaceChildren();
  $("stops").replaceChildren();
  $("performance").textContent = "";
  $("assumptions").replaceChildren(
    text("p", "No current route is available. Correct the request and try again."),
  );
  if (map && layer) {
    map.removeLayer(layer);
    layer = undefined;
    map.setView([39.5, -98.35], 4);
  }
}

function show(result) {
  $("summary").hidden = false;
  $("metrics").replaceChildren();
  const metrics = [
    [`$${result.total_fuel_cost_usd}`, "New fuel purchases"],
    [Number(result.distance_miles).toLocaleString(), "Miles by road"],
    [result.fuel_stops.length, "Fuel stops"],
    [Number(result.fuel_consumed_gallons).toFixed(1), "Gallons consumed"],
  ];
  for (const [value, label] of metrics) {
    const element = text("div", "");
    element.className = "metric";
    element.append(text("strong", value), text("span", label));
    $("metrics").append(element);
  }

  $("stops").replaceChildren();
  for (const stop of result.fuel_stops) {
    const element = text("li", "");
    element.append(
      text("strong", `${stop.name} · ${stop.city}, ${stop.state}`),
      text(
        "span",
        `${Number(stop.gallons).toFixed(2)} gal at $${stop.price_usd_per_gallon}/gal · $${stop.cost_usd}`,
      ),
    );
    $("stops").append(element);
  }
  if (!result.fuel_stops.length) {
    $("stops").append(text("p", "No new fuel is needed with the specified starting tank."));
  }

  const performance = result.performance;
  $("performance").textContent = performance
    ? `${performance.elapsed_ms} ms · ${
        performance.cache_hit ? "cached result" : `${performance.route_api_calls} routing call(s)`
      }`
    : "Saved road-validated route";
  $("response").textContent = JSON.stringify(
    {
      ...result,
      geojson: {
        ...result.geojson,
        features: `${result.geojson.features.length} features; full geometry in API response`,
      },
    },
    null,
    2,
  );
  $("assumptions").replaceChildren(
    text("p", result.cost_basis),
    text("p", result.validation.optimization_scope),
    ...result.warnings.map((warning) => text("p", warning)),
    text("pre", JSON.stringify(result.data, null, 2)),
  );

  if (map) {
    if (layer) map.removeLayer(layer);
    let stopNumber = 0;
    layer = L.geoJSON(result.geojson, {
      style: { color: "#087d78", weight: 5, opacity: 0.85 },
      pointToLayer: (feature, location) => {
        const fuel = feature.properties.kind === "fuel_stop";
        return L.marker(location, {
          icon: L.divIcon({
            className: fuel ? "fuel-dot" : "endpoint-dot",
            html: fuel ? String(++stopNumber) : feature.properties.kind === "start" ? "A" : "B",
            iconSize: [28, 28],
          }),
        });
      },
      onEachFeature: (feature, featureLayer) => {
        if (feature.geometry.type === "Point") {
          const popup = text("div", feature.properties.name);
          if (feature.properties.kind === "fuel_stop") {
            popup.append(
              text("p", `${feature.properties.gallons} gal · $${feature.properties.cost_usd}`),
            );
          }
          featureLayer.bindPopup(popup);
        }
      },
    }).addTo(map);
    map.fitBounds(layer.getBounds(), { padding: [30, 30] });
  }

  $("status").className = "";
  $("status").textContent =
    "Route checked against road distances. Cost covers fuel purchased on this trip.";
}

function parseLocation(value) {
  const parts = value.trim().split(",").map((part) => part.trim());
  const decimal = /^[+-]?(?:\d+(?:\.\d*)?|\.\d+)$/;
  if (parts.length === 2 && parts.every((part) => decimal.test(part))) {
    return { latitude: Number(parts[0]), longitude: Number(parts[1]) };
  }
  return value.trim();
}

function locationInput(location) {
  return location.resolution?.startsWith("user-supplied coordinates")
    ? `${location.latitude}, ${location.longitude}`
    : location.label;
}

$("planner").addEventListener("submit", async (event) => {
  event.preventDefault();
  const body = {
    start: parseLocation($("start").value),
    finish: parseLocation($("finish").value),
    initial_fuel_gallons: Number($("initial").value),
    reserve_gallons: Number($("reserve").value),
  };
  $("request").textContent = JSON.stringify(body, null, 2);
  $("response").textContent = "Waiting for the API response…";
  $("submit").disabled = true;
  $("status").className = "";
  $("status").textContent = "Calculating and validating the road itinerary…";

  let result;
  try {
    const response = await fetch("/api/route/", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    result = await response.json();
    if (!response.ok) {
      throw new Error(result.error?.message || "Request failed.");
    }
    show(result);
  } catch (error) {
    // A previous success must not look like the result of this failed request.
    clearDisplayedPlan();
    $("response").textContent = result
      ? JSON.stringify(result, null, 2)
      : "No valid API response received.";
    $("status").className = "error";
    $("status").textContent = error.message;
  } finally {
    $("submit").disabled = false;
  }
});

if ($("saved-route")) {
  const saved = JSON.parse($("saved-route").textContent);
  $("start").value = locationInput(saved.start);
  $("finish").value = locationInput(saved.finish);
  $("initial").value = saved.initial_fuel_gallons;
  $("reserve").value = saved.reserve_gallons;
  show(saved);
}

if (!map) {
  $("map").append(
    text("p", "Map library unavailable. Route geometry is still included in the API response."),
  );
}
