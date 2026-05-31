from collections import defaultdict
from decimal import Decimal, InvalidOperation

from dcim.models import Device, Rack
from extras.scripts import Script


DEVICE_POWER_FIELD = "power_consumption_watts"
RACK_CONSUMED_FIELD = "rack_power_consumed_watts"
RACK_CAPACITY_FIELD = "rack_power_capacity_watts"
RACK_USAGE_PERCENT_FIELD = "rack_power_usage_percent"


class RackPowerConsumptionFinal(Script):

    class Meta:
        name = "Rack Power Consumption Final"
        description = "Calculate rack electrical consumption, device count, rack usage percentage, and global totals."
        commit_default = False
        job_timeout = 600

    def get_decimal_value(self, value):
        if value in [None, ""]:
            return None

        try:
            return Decimal(str(value))
        except (InvalidOperation, ValueError, TypeError):
            return None

    def get_device_power(self, device):
        custom_fields = device.custom_field_data or {}
        raw_power = custom_fields.get(DEVICE_POWER_FIELD)

        power = self.get_decimal_value(raw_power)

        if power is None:
            return 0

        if power < 0:
            device_name = getattr(device, "name", None) or str(device)
            self.log_warning(
                f"Negative power value '{raw_power}' on device {device_name}. Ignored.",
                obj=device
            )
            return 0

        return int(power)

    def format_usage(self, usage_percent):
        if usage_percent is None:
            return "N/A"
        return f"{usage_percent:.2f}%"

    def escape_table_value(self, value):
        return str(value).replace("|", "\\|")

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

        total_racks = 0
        total_devices_all_racks = 0
        total_consumption_all_racks = 0

        total_devices_with_capacity = 0
        total_consumption_with_capacity = 0
        total_capacity_with_capacity = 0
        racks_with_valid_capacity = 0
        racks_without_valid_capacity = 0

        result_lines = []
        result_lines.append("| Rack | Device Count | Consumption (W) | Capacity (W) | Usage % |")
        result_lines.append("|------|--------------|-----------------|--------------|---------|")

        for rack in Rack.objects.all().order_by("name").iterator():

            total_racks += 1

            rack_name = getattr(rack, "name", None) or str(rack)

            consumed_power = rack_consumption.get(rack.id, 0)
            device_count = rack_device_count.get(rack.id, 0)

            total_devices_all_racks += device_count
            total_consumption_all_racks += consumed_power

            rack.custom_field_data = rack.custom_field_data or {}

            raw_capacity = rack.custom_field_data.get(RACK_CAPACITY_FIELD)
            capacity = self.get_decimal_value(raw_capacity)

            usage_percent = None
            capacity_display = "N/A"

            if capacity is not None and capacity > 0:
                capacity_int = int(capacity)
                capacity_display = str(capacity_int)

                usage_percent = round(
                    Decimal(consumed_power) / capacity * Decimal(100),
                    2
                )

                racks_with_valid_capacity += 1
                total_devices_with_capacity += device_count
                total_consumption_with_capacity += consumed_power
                total_capacity_with_capacity += capacity_int

            else:
                racks_without_valid_capacity += 1

                if raw_capacity not in [None, "", 0, "0"]:
                    self.log_warning(
                        f"Invalid rack capacity value '{raw_capacity}' on rack {rack_name}. Usage percentage not calculated.",
                        obj=rack
                    )

            usage_display = self.format_usage(usage_percent)

            result_lines.append(
                f"| {self.escape_table_value(rack_name)} "
                f"| {device_count} "
                f"| {consumed_power} "
                f"| {capacity_display} "
                f"| {usage_display} |"
            )

            if commit:
                if hasattr(rack, "snapshot"):
                    rack.snapshot()

                rack.custom_field_data[RACK_CONSUMED_FIELD] = consumed_power

                if usage_percent is not None:
                    rack.custom_field_data[RACK_USAGE_PERCENT_FIELD] = float(usage_percent)
                else:
                    rack.custom_field_data.pop(RACK_USAGE_PERCENT_FIELD, None)

                rack.full_clean()
                rack.save()

                self.log_success(
                    f"{rack_name}: {consumed_power} W, {device_count} device(s), capacity={capacity_display} W, usage={usage_display}",
                    obj=rack
                )
            else:
                self.log_info(
                    f"[DRY RUN] {rack_name}: {consumed_power} W, {device_count} device(s), capacity={capacity_display} W, usage={usage_display}",
                    obj=rack
                )

        global_usage_percent = None

        if total_capacity_with_capacity > 0:
            global_usage_percent = round(
                Decimal(total_consumption_with_capacity) / Decimal(total_capacity_with_capacity) * Decimal(100),
                2
            )

        result_lines.append(
            f"| TOTAL ALL RACKS "
            f"| {total_devices_all_racks} "
            f"| {total_consumption_all_racks} "
            f"| N/A "
            f"| N/A |"
        )

        result_lines.append(
            f"| TOTAL RACKS WITH VALID CAPACITY "
            f"| {total_devices_with_capacity} "
            f"| {total_consumption_with_capacity} "
            f"| {total_capacity_with_capacity if total_capacity_with_capacity > 0 else 'N/A'} "
            f"| {self.format_usage(global_usage_percent)} |"
        )

        result_lines.append("")
        result_lines.append("Summary:")
        result_lines.append(f"- Total racks analyzed: {total_racks}")
        result_lines.append(f"- Racks with valid capacity: {racks_with_valid_capacity}")
        result_lines.append(f"- Racks without valid capacity: {racks_without_valid_capacity}")
        result_lines.append(f"- Total devices in all racks: {total_devices_all_racks}")
        result_lines.append(f"- Total consumption across all racks: {total_consumption_all_racks} W")
        result_lines.append(f"- Total valid rack capacity: {total_capacity_with_capacity if total_capacity_with_capacity > 0 else 'N/A'} W")
        result_lines.append(f"- Global usage percentage: {self.format_usage(global_usage_percent)}")
        result_lines.append("")
        result_lines.append("Note: Global usage percentage is calculated only for racks with a valid positive capacity.")

        return "\n".join(result_lines)
