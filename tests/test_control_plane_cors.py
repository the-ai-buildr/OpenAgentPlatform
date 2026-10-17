"""The control plane must be allowed to call localhost from the browser."""

from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.middleware.cors import CORSMiddleware

from app.control_plane import enable_private_network_cors


def test_private_network_preflight_from_the_control_plane_is_allowed():
    app = FastAPI()
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["https://os.agno.com"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    enable_private_network_cors(app)

    response = TestClient(app).options(
        "/config",
        headers={
            "Origin": "https://os.agno.com",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "authorization",
            "Access-Control-Request-Private-Network": "true",
        },
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "https://os.agno.com"
    assert response.headers["access-control-allow-private-network"] == "true"
