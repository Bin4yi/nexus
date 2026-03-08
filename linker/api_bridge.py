"""
linker/api_bridge.py
Two-pass REST API bridge detector.
Pass 1: Registers Spring @RequestMapping/@GetMapping/@PostMapping endpoints
        AND JAX-RS @Path/@GET/@POST endpoints (used by WSO2/OSGi services).
Pass 2: Detects RestTemplate/WebClient/FeignClient AND Apache HttpClient
        outbound calls (org.apache.http — used extensively by WSO2).
Produces REMOTE_CALLS edge data linking LogicUnits across services.
"""
from __future__ import annotations
import re
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class EndpointRegistration:
    """A registered REST endpoint from a Spring controller."""
    path: str
    http_method: str            # GET, POST, PUT, DELETE, PATCH
    handler_fqn: str            # FQN of the handler method
    handler_geid: str
    consumes: str = ""          # e.g. "application/json"
    produces: str = ""          # e.g. "application/json"


@dataclass
class RemoteCallEdge:
    """A detected cross-service REST call to be stored as REMOTE_CALLS."""
    caller_fqn: str
    caller_geid: str
    callee_fqn: str             # FQN of matched handler (best-effort)
    callee_geid: str
    protocol: str = "REST"
    http_method: str = "GET"
    path: str = ""


class ApiBridgeDetector:
    """
    Detects cross-service REST API calls between Java services.

    Two-pass algorithm:
        Pass 1: Scan all .java files for Spring annotations → build endpoint registry
        Pass 2: Scan all .java files for HTTP client calls → match against registry
    """

    # ── Spring MVC mapping annotations → HTTP methods ─────────────────────────
    _MAPPING_PATTERNS = {
        r'@GetMapping\(["\']([^"\']+)["\']': "GET",
        r'@PostMapping\(["\']([^"\']+)["\']': "POST",
        r'@PutMapping\(["\']([^"\']+)["\']': "PUT",
        r'@DeleteMapping\(["\']([^"\']+)["\']': "DELETE",
        r'@PatchMapping\(["\']([^"\']+)["\']': "PATCH",
        r'@RequestMapping\(.*?value\s*=\s*["\']([^"\']+)["\'].*?method\s*=\s*RequestMethod\.(\w+)': "REQUEST",
        r'@RequestMapping\(["\']([^"\']+)["\']': "GET",  # default
    }

    # ── JAX-RS @Path annotation (used by WSO2/OSGi services) ─────────────────
    # @Path appears at class level (base path) and/or method level (sub-path).
    # HTTP method is declared separately via @GET, @POST, etc.
    _JAXRS_PATH_RE = re.compile(r'@Path\s*\(\s*["\']([^"\']+)["\']\s*\)')
    _JAXRS_METHOD_RE = re.compile(r'@(GET|POST|PUT|DELETE|PATCH|HEAD|OPTIONS)\b')
    # JAX-RS content-type annotations
    _CONSUMES_RE = re.compile(r'@Consumes\s*\(\s*["\']?([^"\')\s]+)["\']?\s*\)')
    _PRODUCES_RE = re.compile(r'@Produces\s*\(\s*["\']?([^"\')\s]+)["\']?\s*\)')

    # ── Outbound HTTP client patterns ─────────────────────────────────────────
    # Spring clients
    _CALL_PATTERNS = [
        re.compile(r'restTemplate\.\w+\(\s*["\']([^"\']+)["\']'),
        re.compile(r'webClient\.\w+\(\)\s*\.uri\(\s*["\']([^"\']+)["\']'),
        re.compile(r'\.get\(\s*["\']([^"\']+)["\']'),       # FeignClient
        # Apache HttpClient (org.apache.http) — used by WSO2
        re.compile(r'new\s+HttpGet\s*\(\s*["\']([^"\']+)["\']'),
        re.compile(r'new\s+HttpPost\s*\(\s*["\']([^"\']+)["\']'),
        re.compile(r'new\s+HttpPut\s*\(\s*["\']([^"\']+)["\']'),
        re.compile(r'new\s+HttpDelete\s*\(\s*["\']([^"\']+)["\']'),
        re.compile(r'new\s+HttpPatch\s*\(\s*["\']([^"\']+)["\']'),
        # URI builder patterns common in WSO2
        re.compile(r'\.setURI\s*\(\s*new\s+URI\s*\(\s*["\']([^"\']+)["\']'),
        re.compile(r'URI\.create\s*\(\s*["\']([^"\']+)["\']'),
        re.compile(r'\.target\s*\(\s*["\']([^"\']+)["\']'),  # JAX-RS Client
    ]

    def __init__(self):
        self._registry: list[EndpointRegistration] = []

    def register_endpoints(
        self,
        java_files: list[Path],
        fqn_map: dict[str, str],   # file_path → class FQN
        geid_map: dict[str, str],  # method_fqn → geid
    ) -> None:
        """
        Pass 1: Scan all Java files and register Spring endpoint handlers.

        Args:
            java_files: List of .java source files to scan
            fqn_map:    Maps file paths to their class FQN
            geid_map:   Maps method FQNs to their GEIDs
        """
        self._registry.clear()
        for java_file in java_files:
            try:
                source = java_file.read_text(encoding="utf-8", errors="replace")
                self._scan_endpoints(source, str(java_file), fqn_map, geid_map)
            except OSError as e:
                logger.warning("Could not read %s: %s", java_file, e)

        logger.info("Registered %d endpoints", len(self._registry))

    def detect_calls(
        self,
        java_files: list[Path],
        fqn_map: dict[str, str],
        geid_map: dict[str, str],
    ) -> list[RemoteCallEdge]:
        """
        Pass 2: Detect outbound HTTP calls and match to registered endpoints.

        Returns:
            List of RemoteCallEdge objects to store as [:REMOTE_CALLS] edges
        """
        edges: list[RemoteCallEdge] = []

        for java_file in java_files:
            try:
                source = java_file.read_text(encoding="utf-8", errors="replace")
                edges.extend(
                    self._scan_calls(source, str(java_file), fqn_map, geid_map)
                )
            except OSError as e:
                logger.warning("Could not read %s: %s", java_file, e)

        logger.info("Detected %d cross-service REST calls", len(edges))
        return edges

    # ── Private ───────────────────────────────────────────────────────────────

    def _scan_endpoints(
        self, source: str, file_path: str, fqn_map: dict, geid_map: dict
    ) -> None:
        class_fqn = fqn_map.get(file_path, file_path)

        # ── Spring MVC annotations ────────────────────────────────────────────
        for pattern_str, http_method in self._MAPPING_PATTERNS.items():
            for match in re.finditer(pattern_str, source, re.DOTALL):
                path = match.group(1).strip()
                norm_path = self._normalize_path(path)
                # Extract actual method name from source following the annotation
                method_name = "handler"  # fallback
                pos = match.end()
                method_match = re.search(
                    r'\bpublic\s+[\w<>\[\], ]+\s+(\w+)\s*\(', source[pos:pos + 300]
                )
                if method_match:
                    method_name = method_match.group(1)
                handler_fqn = f"{class_fqn}.{method_name}"
                geid = geid_map.get(handler_fqn, "")
                self._registry.append(
                    EndpointRegistration(
                        path=norm_path,
                        http_method=http_method,
                        handler_fqn=handler_fqn,
                        handler_geid=geid,
                    )
                )

        # ── JAX-RS @Path endpoints (WSO2/OSGi style) ─────────────────────────
        # Strategy: detect class-level @Path (base path), then combine with
        # each method-level @Path to build the effective full path.
        lines = source.splitlines()

        # Detect class-level @Path — appears before the first class body '{'
        first_brace = source.find("{")
        class_header = source[:first_brace] if first_brace != -1 else source
        class_base_match = self._JAXRS_PATH_RE.search(class_header)
        class_base_path = class_base_match.group(1).strip() if class_base_match else ""

        for i, line in enumerate(lines):
            path_match = self._JAXRS_PATH_RE.search(line)
            if not path_match:
                continue
            path_value = path_match.group(1).strip()

            # Skip if this is the class-level @Path (same value as class_base_path)
            if class_base_path and path_value == class_base_path:
                # Only skip if it's in the header (before first '{')
                line_offset = sum(len(l) + 1 for l in lines[:i])
                if line_offset < (first_brace if first_brace != -1 else len(source)):
                    continue

            # Compose effective path: class base + method path
            effective_path = self._normalize_path(class_base_path + "/" + path_value)

            # Scan ±3 lines for HTTP method annotation
            window = lines[max(0, i - 3): i + 4]
            window_text = "\n".join(window)
            method_m = self._JAXRS_METHOD_RE.search(window_text)
            http_method = method_m.group(1) if method_m else "GET"

            # Extract actual method name from nearby lines following @Path
            method_name = "handler"  # fallback
            search_text = "\n".join(lines[i:min(len(lines), i + 5)])
            method_match = re.search(
                r'\bpublic\s+[\w<>\[\], ]+\s+(\w+)\s*\(', search_text
            )
            if method_match:
                method_name = method_match.group(1)

            # Extract @Consumes / @Produces from same window
            consumes_m = self._CONSUMES_RE.search(window_text)
            produces_m = self._PRODUCES_RE.search(window_text)
            consumes = consumes_m.group(1) if consumes_m else ""
            produces = produces_m.group(1) if produces_m else ""

            handler_fqn = f"{class_fqn}.{method_name}"
            geid = geid_map.get(handler_fqn, "")
            self._registry.append(
                EndpointRegistration(
                    path=effective_path,
                    http_method=http_method,
                    handler_fqn=handler_fqn,
                    handler_geid=geid,
                    consumes=consumes,
                    produces=produces,
                )
            )

    def _scan_calls(
        self, source: str, file_path: str, fqn_map: dict, geid_map: dict
    ) -> list[RemoteCallEdge]:
        edges = []
        caller_fqn = fqn_map.get(file_path, file_path)
        caller_geid = geid_map.get(caller_fqn, "")

        for pattern in self._CALL_PATTERNS:
            for match in pattern.finditer(source):
                url = match.group(1)
                path = self._extract_path(url)
                norm_path = self._normalize_path(path)
                matched = self._match_endpoint(norm_path)
                if matched:
                    edges.append(
                        RemoteCallEdge(
                            caller_fqn=caller_fqn,
                            caller_geid=caller_geid,
                            callee_fqn=matched.handler_fqn,
                            callee_geid=matched.handler_geid,
                            http_method=matched.http_method,
                            path=norm_path,
                        )
                    )
        return edges

    def _match_endpoint(
        self, path: str
    ) -> Optional[EndpointRegistration]:
        """Match a detected call path against the endpoint registry."""
        norm = self._normalize_path(path)
        for endpoint in self._registry:
            if self._paths_match(norm, endpoint.path):
                return endpoint
        return None

    def _paths_match(self, call_path: str, registered_path: str) -> bool:
        """
        Check if two paths match, allowing for path variable substitution.
        Also matches when the call path is a prefix (Java string concat drops the var).
        e.g. /api/users/123 matches /api/users/{id}
             /api/users/    matches /api/users/{id}  (trailing slash stripped by _normalize)
             /api/users     matches /api/users/{id}  (concat: "url/" + var → prefix)
        """
        # Convert registered path variables to regex
        pattern = re.sub(r"\{[^}]+\}", r"[^/]+", re.escape(registered_path))
        if re.fullmatch(pattern, call_path):
            return True
        # Prefix match: call_path is a prefix of registered_path up to a path-variable segment
        # Strip the last /{var} segment and check prefix
        prefix = re.sub(r"/\{[^}]+\}$", "", registered_path)
        if prefix and call_path == prefix:
            return True
        return False

    def _normalize_path(self, path: str) -> str:
        """Strip query strings and trailing slashes."""
        path = path.split("?")[0].rstrip("/")
        return path or "/"

    def _extract_path(self, url: str) -> str:
        """Extract path component from a URL or path string."""
        # Handle full URLs: http://host/api/... → /api/...
        match = re.search(r"https?://[^/]+(/.+)", url)
        if match:
            return match.group(1)
        return url if url.startswith("/") else f"/{url}"
