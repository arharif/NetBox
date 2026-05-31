from collections import defaultdict
from decimal import Decimal, InvalidOperation

from dcim.models import Device, Rack
from extras.scripts import Script


DEVICE_POWER_FIELD = "power_consumption_watts"
RACK_CONSUMED_FIELD = "rack_power_consumed_watts"
RACK_CAPACITY_FIELD = "rack_power_capacity_watts"
RACK_USAGE_PERCENT_FIELD = "rack_power_usage_percent"


class RackPowerConsumptionCalculation(Script):

    class Meta:
        name = "Rack Power Consumption Calculation"
        description = "Calculate electrical consumption for each rack."
        commit_default = False

    def get_device_power(self, device):
        custom_fields = device.custom_field_data or {}
        raw_power = custom_fields.get(DEVICE_POWER_FIELD)

        if raw_power in [None, ""]:
            return 0

        try:
            power = Decimal(str(raw_power))
        except (InvalidOperation, ValueError, TypeError):
            self.log_warning(
                f"Invalid power value '{raw_power}' on device {device.name}. Ignored.",
                obj=device
            )
            return 0

        if power < 0:
            self.log_warning(
                f"Negative power value '{raw_power}' on device {device.name}. Ignored.",
                obj=device
            )
            return 0

        return int(power)

    def run(self, data, commit):

        rack_consumption = defaultdict(int)
        rack_device_count = defaultdict(int)

        devices = Device.objects.filter(
            rack__isnull=False
        ).select_related("rack")

        for device in devices.iterator():
            power = self.get_device_power(device)
            rack_consumption[device.rack_id] += power
            rack_device_count[device.rack_id] += 1

        result_lines = []

        for rack in Rack.objects.all().iterator():

            consumed_power = rack_consumption.get(rack.id, 0)
            device_count = rack_device_count.get(rack.id, 0)

            rack.custom_field_data = rack.custom_field_data or {}

            raw_capacity = rack.custom_field_data.get(RACK_CAPACITY_FIELD)
            usage_percent = None

            if raw_capacity not in [None, "", 0, "0"]:
                try:
                    capacity = Decimal(str(raw_capacity))
                    if capacity > 0:
                        usage_percent = round(
                            Decimal(consumed_power) / capacity * 100,
                            2
                        )
                except (InvalidOperation, ValueError, TypeError):
                    self.log_warning(
                        f"Invalid rack capacity value '{raw_capacity}' on rack {rack.name}.",
                        obj=rack
                    )

            rack.custom_field_data[RACK_CONSUMED_FIELD] = consumed_power

            if usage_percent is not None:
                rack.custom_field_data[RACK_USAGE_PERCENT_FIELD] = float(usage_percent)

            message = (
                f"{rack.name}: {consumed_power} W consumed "
                f"across {device_count} device(s)"
            )

            if usage_percent is not None:
                message += f" | Usage: {usage_percent}%"

            if commit:
                rack.full_clean()
                rack.save()
                self.log_success(message, obj=rack)
            else:
                self.log_info("[DRY RUN] " + message, obj=rack)

            result_lines.append(message)

        return "\n".join(result_lines)
