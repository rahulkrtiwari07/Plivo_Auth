import os
import tempfile

# Test configuration must be in place before `app` is imported (settings are cached).
os.environ.update(
    DATABASE_URL=f"sqlite:///{os.path.join(tempfile.mkdtemp(), 'test.db')}",
    PLIVO_MOCK="true", JWT_SECRET="test-secret-0123456789-abcdefghijklmnop", ADMIN_API_KEY="admin",
    DEV_MODE="true", COMPLIANCE_POLL_SECONDS="0", ALLOWED_NUMBER_TYPES="local,tollfree",
    MAX_NUMBERS_PER_CLIENT="2", PRICE_MARKUP="1.5", PUBLIC_BASE_URL="https://numbers.example.com",
    PLIVO_USE_SUBACCOUNTS="true", KYC_DOCUMENTS_MODE="all", NUMBER_CITIES="Bangalore,Mumbai",
    INTEGRATION_API_KEY="int-key", WEBHOOK_URL="https://bigship.example.com/hooks/plivo-numbers",
    WEBHOOK_SECRET="whsec-test", WEBHOOK_POLL_SECONDS="3600",  # tests deliver explicitly
)
