import unittest

from pihub.bt_le.ble_serial import DongleState
from pihub.bt_le.controller import BTLEController


class _DummySerialHandle:
    is_open = True


class TestBTLEControllerStatus(unittest.TestCase):
    def test_status_shape_defaults(self) -> None:
        bt = BTLEController()

        status = bt.status

        self.assertEqual(
            set(status.keys()),
            {
                "adapter_present",
                "ready",
                "advertising",
                "connected",
                "proto_boot",
                "error",
                "conn_params",
                "phy",
                "last_disc_reason",
            },
        )
        self.assertFalse(status["adapter_present"])
        self.assertFalse(status["ready"])
        self.assertFalse(status["advertising"])
        self.assertFalse(status["connected"])
        self.assertFalse(status["proto_boot"])
        self.assertFalse(status["error"])
        self.assertIsNone(status["conn_params"])
        self.assertIsNone(status["phy"])
        self.assertIsNone(status["last_disc_reason"])

    def test_status_updates_from_evt_backed_state(self) -> None:
        bt = BTLEController()

        st = DongleState(
            ready=True,
            advertising=True,
            connected=True,
            proto_boot=True,
            error=True,
            conn_params={"interval_ms_x100": 3000, "latency": 0, "timeout_ms": 720},
            phy={"tx": "2M", "rx": "2M"},
            last_disc_reason=19,
        )

        # Simulate all incoming EVT kinds the dongle parser emits.
        for event in ["READY", "ADV", "CONN", "PROTO", "CONN_PARAMS", "PHY", "DISC", "ERR"]:
            bt._on_dongle_event(event.lower(), st)

        # Pretend serial device exists to verify adapter_present.
        bt._serial._ser = _DummySerialHandle()  # type: ignore[attr-defined]

        status = bt.status

        self.assertTrue(status["adapter_present"])
        self.assertTrue(status["ready"])
        self.assertTrue(status["advertising"])
        self.assertTrue(status["connected"])
        self.assertTrue(status["proto_boot"])
        self.assertTrue(status["error"])
        self.assertEqual(status["conn_params"], st.conn_params)
        self.assertEqual(status["phy"], st.phy)
        self.assertEqual(status["last_disc_reason"], 19)


if __name__ == "__main__":
    unittest.main()
