import numpy as np

# LiPo cell voltages corresponding to battery percentage from 0% to 100% in 5% increments
LIPO_CELL_RELATION = [3.27, 3.61, 3.69, 3.71, 3.73, 3.75, 3.77, 3.79, 3.8, 3.82, 3.84, 3.85, 3.87, 3.91, 3.95, 3.98, 4.02, 4.08, 4.11, 4.15, 4.2]

class BatteryManager:
    def __init__(self, num_cells: int) -> None:
        self.num_cells = num_cells
        self.voltage_steps = np.array(LIPO_CELL_RELATION)
        self.percentage_steps = np.arange(0, 101, 5)  # 0% to 100% in 5% steps

    def get_percentage(self, total_voltage: float) -> float:
        """
        Calculate battery percentage based on total voltage.
        
        Args:
            total_voltage (float): The measured total voltage of the battery
            
        Returns:
            float: Estimated battery percentage (0-1)
        """
        voltage_per_cell = total_voltage / self.num_cells
        percentage = np.interp(voltage_per_cell, self.voltage_steps, self.percentage_steps)
        return float(percentage / 100.0)

