"""
Auth routes package — combines sub-routers into a single router for app.py.
"""

from fastapi import APIRouter

from routes.auth.device import router as device_router
from routes.auth.onboarding import router as onboarding_router
from routes.auth.password import router as password_router
from routes.auth.routes import router as core_router
from routes.auth.verification import router as verification_router

router = APIRouter(tags=["Authentication"])
router.include_router(core_router)
router.include_router(verification_router)
router.include_router(password_router)
router.include_router(device_router)
router.include_router(onboarding_router)
