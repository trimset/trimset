from trimset_server.migrate import available


def test_migrations_ship_inside_the_package_in_order() -> None:
    shipped = available()
    versions = [v for v, _ in shipped]
    assert versions == sorted(versions)
    assert versions[0] == "0001_capture"
    assert b"CREATE TABLE decisions" in dict(shipped)["0001_capture"]
