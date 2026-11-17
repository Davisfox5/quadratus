import pytest
from stockroom.store import Store, StoreError


def test_add_and_list(tmp_path):
    store = Store(tmp_path / "s.db")
    store.add_item("A-1", "Anvil", 3)
    store.add_item("B-2", "Bucket", 0)
    assert store.list_items() == [
        {"sku": "A-1", "name": "Anvil", "quantity": 3},
        {"sku": "B-2", "name": "Bucket", "quantity": 0},
    ]


def test_duplicate_sku_is_refused(tmp_path):
    store = Store(tmp_path / "s.db")
    store.add_item("A-1", "Anvil", 3)
    with pytest.raises(StoreError) as refused:
        store.add_item("A-1", "Another", 1)
    assert refused.value.code == "duplicate_sku"


@pytest.mark.parametrize("quantity", [-1, 1.5, "3", True, None])
def test_bad_quantity_is_refused(tmp_path, quantity):
    store = Store(tmp_path / "s.db")
    with pytest.raises(StoreError) as refused:
        store.add_item("A-1", "Anvil", quantity)
    assert refused.value.code == "invalid_quantity"
    assert store.list_items() == []
