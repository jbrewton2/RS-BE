param()

$ErrorActionPreference="Stop"

docker exec -it css-backend python -c "from main import app; p=getattr(app.state,'providers',None); print('providers type:', type(p)); print('attrs:', sorted([a for a in dir(p) if not a.startswith('_')]))"