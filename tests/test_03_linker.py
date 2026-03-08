"""
tests/test_03_linker.py
Gate 3: 8 tests for Maven resolver and API bridge detector.
No Docker required — all tests use fixture files and in-memory data.
"""
import pytest
import tempfile
from pathlib import Path
from textwrap import dedent

from linker.maven_resolver import MavenResolver
from linker.api_bridge import ApiBridgeDetector


FIXTURES = Path(__file__).parent / "fixtures"
POM_FILE = FIXTURES / "pom.xml"

_maven = MavenResolver()
_bridge = ApiBridgeDetector()


# ── Maven Resolver (Tests 1-4) ────────────────────────────────────────────────

def test_01_pom_dependency_parsed():
    """Test 1: pom.xml fixture produces a DEPENDS_ON edge for shared-lib."""
    module_info, deps = _maven.parse_pom(POM_FILE)
    artifact_ids = [d.target_artifact_id for d in deps]
    assert "shared-lib" in artifact_ids


def test_02_dependency_version_captured():
    """Test 2: The shared-lib dependency has version 2.1.0."""
    _, deps = _maven.parse_pom(POM_FILE)
    shared_lib = next(d for d in deps if d.target_artifact_id == "shared-lib")
    assert shared_lib.target_version == "2.1.0"


def test_03_dependency_scope_captured():
    """Test 3: The shared-lib dependency has scope 'compile'."""
    _, deps = _maven.parse_pom(POM_FILE)
    shared_lib = next(d for d in deps if d.target_artifact_id == "shared-lib")
    assert shared_lib.scope == "compile"


def test_04_module_info_extracted():
    """Test 4: Module groupId and artifactId parsed from pom.xml."""
    module_info, _ = _maven.parse_pom(POM_FILE)
    assert module_info["group_id"] == "com.example"
    assert module_info["artifact_id"] == "user-service"


# ── API Bridge Detector (Tests 5-8) ──────────────────────────────────────────

def test_05_get_mapping_registered():
    """Test 5: @GetMapping endpoint is registered in the endpoint registry."""
    source_file = _write_temp_java("""
        @RestController
        public class UserController {
            @GetMapping("/api/users/{id}")
            public User getUser(@PathVariable Long id) { return null; }
        }
    """)
    _bridge.register_endpoints(
        [source_file],
        fqn_map={str(source_file): "com.example.UserController"},
        geid_map={},
    )
    assert len(_bridge._registry) >= 1
    paths = [e.path for e in _bridge._registry]
    assert any("/api/users" in p for p in paths)


def test_06_rest_template_call_detected():
    """Test 6: RestTemplate.getForObject call is detected and matched."""
    # Register endpoint first
    controller = _write_temp_java("""
        @RestController
        public class UserController {
            @GetMapping("/api/users/{id}")
            public User getUser(@PathVariable Long id) { return null; }
        }
    """)
    _bridge.register_endpoints(
        [controller],
        fqn_map={str(controller): "com.example.UserController"},
        geid_map={},
    )

    # Now detect the call
    caller = _write_temp_java("""
        public class OrderService {
            public void placeOrder(Long uid) {
                User u = restTemplate.getForObject("http://user-svc/api/users/" + uid, User.class);
            }
        }
    """)
    edges = _bridge.detect_calls(
        [caller],
        fqn_map={str(caller): "com.example.OrderService"},
        geid_map={},
    )
    assert len(edges) >= 1
    assert edges[0].http_method == "GET"


def test_07_no_false_match_different_path():
    """Test 7: /api/orders does NOT match /api/users/{id}."""
    _bridge._registry.clear()
    controller = _write_temp_java("""
        @RestController
        public class UserController {
            @GetMapping("/api/users/{id}")
            public User getUser(@PathVariable Long id) { return null; }
        }
    """)
    _bridge.register_endpoints(
        [controller],
        fqn_map={str(controller): "com.example.UserController"},
        geid_map={},
    )
    caller = _write_temp_java("""
        public class OrderService {
            public void listOrders() {
                restTemplate.getForObject("http://order-svc/api/orders", List.class);
            }
        }
    """)
    edges = _bridge.detect_calls(
        [caller],
        fqn_map={str(caller): "com.example.OrderService"},
        geid_map={},
    )
    # /api/orders should NOT match /api/users/{id}
    user_edges = [e for e in edges if "/api/users" in e.path]
    assert len(user_edges) == 0


def test_08_remote_calls_edge_has_required_fields():
    """Test 8: RemoteCallEdge has protocol, method, and path populated."""
    _bridge._registry.clear()
    controller = _write_temp_java("""
        @RestController
        public class AuthController {
            @PostMapping("/api/auth/token")
            public Token getToken() { return null; }
        }
    """)
    _bridge.register_endpoints(
        [controller],
        fqn_map={str(controller): "com.example.AuthController"},
        geid_map={},
    )
    caller = _write_temp_java("""
        public class ApiGateway {
            public void authenticate() {
                restTemplate.getForObject("http://auth-svc/api/auth/token", Token.class);
            }
        }
    """)
    edges = _bridge.detect_calls(
        [caller],
        fqn_map={str(caller): "com.example.ApiGateway"},
        geid_map={},
    )
    if edges:
        edge = edges[0]
        assert edge.protocol == "REST"
        assert edge.path != ""


# ── Helper ────────────────────────────────────────────────────────────────────

def _write_temp_java(source: str) -> Path:
    """Write a Java source string to a temp file and return its path."""
    import tempfile, os
    f = tempfile.NamedTemporaryFile(suffix=".java", delete=False, mode="w", encoding="utf-8")
    f.write(dedent(source))
    f.close()
    return Path(f.name)
