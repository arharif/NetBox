from collections import defaultdict
from decimal import Decimal, InvalidOperation

from dcim.models import Device, Rack
from extras.scripts import Script


DEVICE_POWER_FIELD = "Power_Reserved_Watts"
DEVICE_POWER_FIELD_ALIASES = [
    "Power_Reserved_Watts",
    "power_reserved_watts",
    "Power Reserved Watts",
    "power_consumption_watts",
]

RACK_TOTAL_POWER_FIELD = "Rack_Total_Power"
RACK_TOTAL_POWER_FIELD_ALIASES = [
    "Rack_Total_Power",
    "rack_total_power",
    "rack_total_power_watts",
]

RACK_POWER_PERCENT_FIELD = "Rack_Power_Percentage"
RACK_POWER_PERCENT_FIELD_ALIASES = [
    "Rack_Power_Percentage",
    "rack_power_percentage",
    "rack_power_usage_percent",
]


class RackPowerReservedElite(Script):

    class Meta:
        name = "Rack Power Reserved Elite"
        description = "Calculate reserved electrical power per rack, device count, percentage of global consumption, and total consumption."
        commit_default = False
        job_timeout = 600

    def get_custom_field_value(self, obj, field_names):
        custom_fields = obj.custom_field_data or {}

        for field_name in field_names:
            if field_name in custom_fields:
                return custom_fields.get(field_name)

        return None

    def set_custom_field_value(self, obj, field_names, value):
        obj.custom_field_data = obj.custom_field_data or {}

        for field_name in field_names:
            if field_name in obj.custom_field_data:
                obj.custom_field_data[field_name] = value
                return field_name

        primary_field_name = field_names[0]
        obj.custom_field_data[primary_field_name] = value
        return primary_field_name

    def parse_power_value(self, value):
        if value in [None, ""]:
            return 0, "empty"

        try:
            power = Decimal(str(value).strip())
        except (InvalidOperation, ValueError, TypeError):
            return 0, "invalid"

        if power < 0:
            return 0, "negative"

        return int(power), "valid"

    def format_percentage(self, value):
        if value is None:
            return "0.00%"
        return f"{value:.2f}%"

    def escape_table_value(self, value):
        return str(value).replace("|", "\\|")

    def run(self, data, commit):

        rack_power = defaultdict(int)
        rack_device_count = defaultdict(int)

        total_devices_with_power_value = 0
        total_devices_without_power_value = 0
        invalid_device_values = 0
        negative_device_values = 0

        devices = Device.objects.filter(
            rack__isnull=False
        ).select_related("rack")

        for device in devices.iterator():

            raw_power = self.get_custom_field_value(
                device,
                DEVICE_POWER_FIELD_ALIASES
            )

            power, status = self.parse_power_value(raw_power)

            if status == "valid":
                total_devices_with_power_value += 1

            elif status == "empty":
                total_devices_without_power_value += 1

            elif status == "invalid":
                invalid_device_values += 1
                self.log_warning(
                    f"Invalid power value '{raw_power}' ignored on device {device.name}.",
                    obj=device
                )

            elif status == "negative":
                negative_device_values += 1
                self.log_warning(
                    f"Negative power value '{raw_power}' ignored on device {device.name}.",
                    obj=device
                )

            rack_power[device.rack_id] += power
            rack_device_count[device.rack_id] += 1

        total_racks = 0
        total_devices = 0
        total_reserved_power = sum(rack_power.values())

        result_lines = []
        result_lines.append("| Rack | Device Count | Rack Reserved Power (W) | % of Global Power |")
        result_lines.append("|------|--------------|--------------------------|-------------------|")

        for rack in Rack.objects.all().order_by("name").iterator():

            total_racks += 1

            rack_name = getattr(rack, "name", None) or str(rack)
            device_count = rack_device_count.get(rack.id, 0)
            reserved_power = rack_power.get(rack.id, 0)

            total_devices += device_count

            if total_reserved_power > 0:
                rack_percentage = round(
                    Decimal(reserved_power) / Decimal(total_reserved_power) * Decimal(100),
                    2
                )
            else:
                rack_percentage = Decimal(0)

            percentage_display = self.format_percentage(rack_percentage)

            result_lines.append(
                f"| {self.escape_table_value(rack_name)} "
                f"| {device_count} "
                f"| {reserved_power} "
                f"| {percentage_display} |"
            )

            rack.custom_field_data = rack.custom_field_data or {}

            previous_total_power = self.get_custom_field_value(
                rack,
                RACK_TOTAL_POWER_FIELD_ALIASES
            )

            previous_percentage = self.get_custom_field_value(
                rack,
                RACK_POWER_PERCENT_FIELD_ALIASES
            )

            if commit:
                if hasattr(rack, "snapshot"):
                    rack.snapshot()

                total_power_field_used = self.set_custom_field_value(
                    rack,
                    RACK_TOTAL_POWER_FIELD_ALIASES,
                    reserved_power
                )

                percentage_field_used = self.set_custom_field_value(
                    rack,
                    RACK_POWER_PERCENT_FIELD_ALIASES,
                    float(rack_percentage)
                )

                rack.full_clean()
                rack.save()

                self.log_success(
                    f"{rack_name}: {total_power_field_used} updated from {previous_total_power} to {reserved_power} W; "
                    f"{percentage_field_used} updated from {previous_percentage} to {percentage_display}; "
                    f"devices={device_count}.",
                    obj=rack
                )

            else:
                self.log_info(
                    f"[DRY RUN] {rack_name}: {RACK_TOTAL_POWER_FIELD} would be updated from {previous_total_power} to {reserved_power} W; "
                    f"{RACK_POWER_PERCENT_FIELD} would be updated from {previous_percentage} to {percentage_display}; "
                    f"devices={device_count}.",
                    obj=rack
                )

        result_lines.append(
            f"| **TOTAL ALL RACKS** "
            f"| **{total_devices}** "
            f"| **{total_reserved_power}** "
            f"| **100.00%** |"
        )

        result_lines.append("")
        result_lines.append("Summary:")
        result_lines.append(f"- Total racks analyzed: {total_racks}")
        result_lines.append(f"- Total devices in racks: {total_devices}")
        result_lines.append(f"- Total reserved power across all racks: {total_reserved_power} W")
        result_lines.append(f"- Devices with valid power value: {total_devices_with_power_value}")
        result_lines.append(f"- Devices without power value: {total_devices_without_power_value}")
        result_lines.append(f"- Invalid device power values ignored: {invalid_device_values}")
        result_lines.append(f"- Negative device power values ignored: {negative_device_values}")
        result_lines.append("")
        result_lines.append("Custom fields used:")
        result_lines.append(f"- Device power source field: {DEVICE_POWER_FIELD}")
        result_lines.append(f"- Rack total power field: {RACK_TOTAL_POWER_FIELD}")
        result_lines.append(f"- Rack percentage field: {RACK_POWER_PERCENT_FIELD}")
        result_lines.append("")
        result_lines.append("Note:")
        result_lines.append("- Rack Reserved Power is calculated from device Power_Reserved_Watts values.")
        result_lines.append("- % of Global Power = Rack Reserved Power / Total Reserved Power of all racks.")
        result_lines.append("- This is reserved/documented power, not live PDU measurement.")

        return "\n".join(result_lines)
