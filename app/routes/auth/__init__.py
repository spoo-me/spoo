"""
Auth routes package — combines sub-routers into a single router for factory.py.
"""

from fastapi import APIRouter

from app.routes.auth.device import router as device_router
from app.routes.auth.onboarding import router as onboarding_router
from app.routes.auth.password import router as password_router
from app.routes.auth.routes import router as core_router
from app.routes.auth.verification import router as verification_router

router = APIRouter(tags=["Authentication"])
router.include_router(core_router)
router.include_router(verification_router)
router.include_router(password_router)
router.include_router(device_router)
router.include_router(onboarding_router)
