# generate_dev_token.py
from datetime import datetime, timedelta, timezone
from jose import jwt
 
secret = "dev-secret-change-me"   # must match JWT_SECRET
alg = "HS256"
 
claims = {
    "sub": "test.user",
    "aud": "web",
    "exp": datetime.now(timezone.utc) + timedelta(hours=1),
    # Other fields like in Java if you want:
    "created": datetime.now(timezone.utc).isoformat(),
    "tfaEnabled": False,
    "participant": {"id": 123},
    "user": {"id": 1, "name": "Test User"},
    "origin": "DEV",
}
 
token = jwt.encode(claims, secret, algorithm=alg)
print(token)

# eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiJ0ZXN0LnVzZXIiLCJhdWQiOiJ3ZWIiLCJleHAiOjE3NjU1MzM5MzIsImNyZWF0ZWQiOiIyMDI1LTEyLTEyVDA5OjA1OjMyLjE0MTM5OCswMDowMCIsInRmYUVuYWJsZWQiOmZhbHNlLCJwYXJ0aWNpcGFudCI6eyJpZCI6MTIzfSwidXNlciI6eyJpZCI6MSwibmFtZSI6IlRlc3QgVXNlciJ9LCJvcmlnaW4iOiJERVYifQ.WhHN34JhaJPPaZyqVsvVUVsxS6vFBd4Yk83SC6xpR7M