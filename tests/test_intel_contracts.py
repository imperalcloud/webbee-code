from webbee.intel.contracts import extract_contracts, match_contracts


def test_extracts_python_http_endpoints_with_provenance():
    text = '''
@router.post("/v1/orders/{order_id}", response_model=OrderOut)
async def create_order(order_id: int, body: OrderIn):
    return service.create(body)

@app.get('/healthz')
def health():
    return {"ok": True}
'''
    endpoints, schemas = extract_contracts("api/orders.py", text, "python")
    assert [(e.method, e.route, e.handler) for e in endpoints] == [
        ("POST", "/v1/orders/{order_id}", "create_order"),
        ("GET", "/healthz", "health"),
    ]
    assert endpoints[0].path == "api/orders.py"
    assert endpoints[0].line == 2
    assert "OrderIn" in endpoints[0].schema_refs
    assert "OrderOut" in endpoints[0].schema_refs


def test_extracts_pydantic_and_sqlalchemy_schemas():
    text = '''
class OrderIn(BaseModel):
    sku: str
    quantity: int = 1

class OrderRow(Base):
    __tablename__ = "orders"
    id = Column(Integer, primary_key=True)
    payload = mapped_column(JSON)
'''
    endpoints, schemas = extract_contracts("models.py", text, "python")
    assert not endpoints
    by_name = {s.name: s for s in schemas}
    assert by_name["OrderIn"].schema_kind == "model"
    assert by_name["OrderIn"].fields == ("quantity", "sku")
    assert by_name["OrderRow"].schema_kind == "table"
    assert by_name["OrderRow"].storage_name == "orders"
    assert by_name["OrderRow"].fields == ("id", "payload")


def test_extracts_typescript_express_and_interfaces():
    text = '''
router.put("/v1/users/:id", updateUser)
export interface UserPatch {
  displayName?: string;
  active: boolean;
}
'''
    endpoints, schemas = extract_contracts("routes/users.ts", text, "typescript")
    assert [(e.method, e.route, e.handler) for e in endpoints] == [
        ("PUT", "/v1/users/:id", "updateUser")
    ]
    assert schemas[0].name == "UserPatch"
    assert schemas[0].fields == ("active", "displayName")


def test_cross_repo_matching_is_deterministic_and_typed():
    producer_eps, producer_schemas = extract_contracts(
        "api.py",
        '@app.post("/v1/orders", response_model=OrderOut)\ndef create(body: OrderIn): pass\n',
        "python",
    )
    _, client_schemas = extract_contracts(
        "client.py", "class OrderIn(BaseModel):\n    sku: str\n", "python"
    )
    links = match_contracts(
        [
            {"repo_key": "producer", "endpoints": producer_eps, "schemas": producer_schemas},
            {"repo_key": "client", "endpoints": [], "schemas": client_schemas},
        ]
    )
    schema_links = [x for x in links if x["relation"] == "schema_name_match"]
    assert schema_links
    assert schema_links[0]["source_repo"] == "client"
    assert schema_links[0]["target_repo"] == "producer"


def test_contract_slice_filters_endpoint_and_schema_evidence(tmp_path):
    from webbee.intel.service import IntelService
    from webbee.intel.query import contract_slice

    (tmp_path / "api.py").write_text('''
class OrderIn(BaseModel):
    sku: str

@router.post("/v1/orders", response_model=OrderOut)
def create_order(body: OrderIn): pass
''')
    svc = IntelService(str(tmp_path), "orders", cache_dir=str(tmp_path / "cache"))
    svc.build()
    out = contract_slice(svc, "order", kind="all", k=20)
    items = out["data"]["items"]
    assert any(x["kind"] == "endpoint" and x["route"] == "/v1/orders" for x in items)
    assert any(x["kind"] == "schema" and x["name"] == "OrderIn" for x in items)
    assert all(x["repo_key"] == "orders" for x in items)
    assert all(x["path"] and x["line"] > 0 for x in items)
