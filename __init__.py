# GCode Analyzer Plugin for Cura
# Compatible with Cura 5.x (PyQt6)

import re
import math
import os
from PyQt6.QtWidgets import QFileDialog, QMessageBox
from PyQt6.QtCore import Qt
from UM.Extension import Extension
from UM.Logger import Logger

try:
    from cura.CuraApplication import CuraApplication
except ImportError:
    CuraApplication = None

try:
    from cura.CuraApplication import CuraApplication
except ImportError:
    CuraApplication = None

class GCodeAnalyzer(Extension):
    def __init__(self, app):
        super().__init__()
        self._app = app
        
        # Récupérer les préférences
        from UM.Application import Application
        self._preferences = Application.getInstance().getPreferences()
        
        # Initialiser la préférence pour le dernier répertoire
        self._preferences.addPreference("gcode_analyzer/last_directory", "")
        
        self.setMenuName("GCode Analyzer")
        self.addMenuItem("Analyze a GCode file", self.analyzeFile)
        if CuraApplication is not None:
            self.addMenuItem("Analyze current GCode", self.analyzeCurrentGcode)
        Logger.log("i", "GCodeAnalyzer plugin initialized successfully")
    
    def analyzeFile(self):
        Logger.log("i", "analyzeFile() called!")
        try:
            Logger.log("i", "Opening file dialog...")
            
            # Récupérer le dernier répertoire utilisé
            last_directory = self._preferences.getValue("gcode_analyzer/last_directory")
            if not last_directory or not os.path.exists(last_directory):
                last_directory = os.path.expanduser("~")  # Répertoire home par défaut
            
            file_path, _ = QFileDialog.getOpenFileName(
                None,
                "Select a GCode file",
                last_directory,
                "GCode files (*.gcode *.nc *.ngc);;All files (*.*)"
            )
            Logger.log("i", "File selected: " + str(file_path))
            if file_path:
                # Sauvegarder le répertoire pour la prochaine fois
                directory = os.path.dirname(file_path)
                self._preferences.setValue("gcode_analyzer/last_directory", directory)
                Logger.log("i", "Saved directory: " + directory)
                
                Logger.log("i", "Analyzing file: " + str(file_path))
                stats = self._analyze_gcode_file(file_path)
                if stats:
                    self._display_results(stats, file_path)
        except Exception as e:
            Logger.log("e", "Error in analyzeFile: " + str(e))
            self._display_error("Error", "An error occurred: " + str(e))
    
    def analyzeCurrentGcode(self):
        try:
            if CuraApplication is None:
                self._display_error("Not available", "This feature requires Cura application")
                return
            
            application = CuraApplication.getInstance()
            if application is None:
                self._display_error("Error", "Cannot access Cura application")
                return
            
            # Vérifier qu'il y a un modèle chargé
            scene = application.getController().getScene()
            if not scene or not scene.getRoot() or not scene.getRoot().hasChildren():
                self._display_error("No model loaded", "Please load a model and slice it before analyzing the GCode.")
                return
            
            # Récupérer le GCode depuis le backend
            backend = application.getBackend()
            if backend is None:
                self._display_error("Error", "Cannot access backend")
                return
            
            # Le vrai GCode est dans scene.gcode_dict[plate_id] mais c'est une liste d'une seule string géante
            gcode_dict = getattr(scene, "gcode_dict", None)
            if not gcode_dict:
                self._display_error("GCode not available", "Please slice the model first (click the 'Slice' button).")
                return
            
            Logger.log("i", "Found gcode_dict with keys: " + str(gcode_dict.keys()))
            
            # Concaténer tout le GCode
            full_gcode = ""
            for plate_id in gcode_dict:
                if gcode_dict[plate_id]:
                    # Chaque élément de la liste est une grosse string
                    for gcode_chunk in gcode_dict[plate_id]:
                        full_gcode += str(gcode_chunk)
            
            if not full_gcode:
                self._display_error("Empty GCode", "The generated GCode is empty.")
                return
            
            Logger.log("i", "Total GCode length: " + str(len(full_gcode)) + " characters")
            
            # Diviser en lignes
            gcode_lines = full_gcode.split('\n')
            Logger.log("i", "Split into " + str(len(gcode_lines)) + " lines")
            
            stats = self._analyze_gcode_list(gcode_lines)
            if stats:
                self._display_results(stats, "Current GCode (Cura)")
            else:
                self._display_error("Analysis failed", "Could not analyze the GCode. Please try again.")
        except Exception as e:
            Logger.log("e", "Error analyzing current GCode: " + str(e))
            import traceback
            Logger.log("e", traceback.format_exc())
            self._display_error("Error", "Unable to analyze GCode: " + str(e))
    
    def _analyze_gcode_file(self, gcode_file):
        try:
            with open(gcode_file, 'r', encoding='utf-8', errors='ignore') as f:
                lines = f.readlines()
            return self._analyze_gcode_list(lines)
        except Exception as e:
            Logger.log("e", "Error reading file: " + str(e))
            self._display_error("Read error", "Unable to read file: " + str(e))
            return None
    
    def _analyze_gcode_list(self, lines):
        x_current = 0.0
        y_current = 0.0
        z_current = 0.0
        e_current = 0.0
        e_max = 0.0  # Valeur max de E pour le filament total
        e_min = 0.0  # Valeur min de E (pour les rétractions)
        x_moves = 0
        y_moves = 0
        z_moves = 0
        x_distance = 0.0
        y_distance = 0.0
        z_distance = 0.0
        total_distance = 0.0
        extrusion_distance = 0.0
        extrusion_moves = 0
        travel_distance = 0.0
        travel_moves = 0
        
        speeds = []
        current_speed = 0.0
        
        # Détecter le mode de positionnement E (absolu ou relatif)
        e_absolute = True  # Par défaut, la plupart des GCode utilisent le mode absolu
        
        Logger.log("i", "Starting analysis of " + str(len(lines)) + " lines")
        
        for idx, line in enumerate(lines):
            try:
                # Convertir en string si ce n'est pas déjà le cas
                line = str(line).strip()
                
                if not line or line.startswith(';'):
                    continue
                
                # Détecter le changement de mode de positionnement
                if line.startswith('M82'):
                    e_absolute = True
                    Logger.log("i", "E positioning: absolute")
                elif line.startswith('M83'):
                    e_absolute = False
                    Logger.log("i", "E positioning: relative")
                
                # Chercher les commandes de vitesse (F)
                speed_match = re.search(r'F(\d+\.?\d*)', line)
                if speed_match:
                    current_speed = float(speed_match.group(1)) / 60.0  # Convertir de mm/min à mm/s
                    speeds.append(current_speed)
                
                # Chercher les commandes G0 ou G1
                if line.startswith('G0') or line.startswith('G1') or ' G0 ' in line or ' G1 ' in line:
                    x_match = re.search(r'X([-+]?\d*\.?\d+)', line)
                    y_match = re.search(r'Y([-+]?\d*\.?\d+)', line)
                    z_match = re.search(r'Z([-+]?\d*\.?\d+)', line)
                    e_match = re.search(r'E([-+]?\d*\.?\d+)', line)
                    
                    x_new = float(x_match.group(1)) if x_match else x_current
                    y_new = float(y_match.group(1)) if y_match else y_current
                    z_new = float(z_match.group(1)) if z_match else z_current
                    
                    if e_match:
                        e_value = float(e_match.group(1))
                        if e_absolute:
                            e_new = e_value
                            delta_e = e_new - e_current
                        else:
                            delta_e = e_value
                            e_new = e_current + delta_e
                    else:
                        e_new = e_current
                        delta_e = 0.0
                    
                    delta_x = abs(x_new - x_current)
                    delta_y = abs(y_new - y_current)
                    delta_z = abs(z_new - z_current)
                    
                    if x_match:
                        x_moves += 1
                        x_distance += delta_x
                    
                    if y_match:
                        y_moves += 1
                        y_distance += delta_y
                    
                    if z_match:
                        z_moves += 1
                        z_distance += delta_z
                    
                    distance_3d = math.sqrt(delta_x**2 + delta_y**2 + delta_z**2)
                    total_distance += distance_3d
                    
                    # Distinguer extrusion vs déplacement
                    if e_match and delta_e > 0:  # Extrusion (E augmente)
                        extrusion_distance += distance_3d
                        extrusion_moves += 1
                    elif distance_3d > 0:  # Déplacement sans extrusion
                        travel_distance += distance_3d
                        travel_moves += 1
                    
                    # Suivre le maximum de E pour le filament total
                    if e_new > e_max:
                        e_max = e_new
                    if e_new < e_min:
                        e_min = e_new
                    
                    x_current = x_new
                    y_current = y_new
                    z_current = z_new
                    e_current = e_new
                    
            except Exception as e:
                Logger.log("w", "Error processing line " + str(idx) + ": " + str(e))
                continue
        
        # Calculer les statistiques de vitesse
        speed_min = min(speeds) if speeds else 0.0
        speed_max = max(speeds) if speeds else 0.0
        speed_avg = sum(speeds) / len(speeds) if speeds else 0.0
        
        # Calculer le ratio extrusion/déplacement
        if total_distance > 0:
            extrusion_ratio = (extrusion_distance / total_distance) * 100
        else:
            extrusion_ratio = 0.0
        
        # Filament utilisé = différence entre max et min (pour tenir compte des rétractions)
        filament_used = e_max - e_min
        
        Logger.log("i", "Analysis complete: x_moves=" + str(x_moves) + " y_moves=" + str(y_moves) + " z_moves=" + str(z_moves))
        Logger.log("i", "E values: min=" + str(e_min) + " max=" + str(e_max) + " filament=" + str(filament_used))
        
        return {
            'x_moves': x_moves,
            'y_moves': y_moves,
            'z_moves': z_moves,
            'x_distance': round(x_distance, 2),
            'y_distance': round(y_distance, 2),
            'z_distance': round(z_distance, 2),
            'total_distance': round(total_distance, 2),
            'extrusion_distance': round(extrusion_distance, 2),
            'travel_distance': round(travel_distance, 2),
            'extrusion_moves': extrusion_moves,
            'travel_moves': travel_moves,
            'extrusion_ratio': round(extrusion_ratio, 2),
            'filament_used': round(filament_used, 2),
            'speed_min': round(speed_min, 2),
            'speed_max': round(speed_max, 2),
            'speed_avg': round(speed_avg, 2),
            'layer_count': z_moves
        }
    
    def _format_distance(self, distance_mm):
        """Formatte une distance en choisissant l'unité appropriée (mm, m, km)"""
        if distance_mm < 1000:  # Moins de 1 mètre
            return f"{distance_mm:.2f} mm"
        elif distance_mm < 1000000:  # Moins de 1 km
            return f"{distance_mm / 1000:.2f} m"
        else:  # 1 km ou plus
            return f"{distance_mm / 1000000:.2f} km"
    
    def _display_results(self, stats, source):
        try:
            # Formater les distances avec les bonnes unités
            x_distance_str = self._format_distance(stats['x_distance'])
            y_distance_str = self._format_distance(stats['y_distance'])
            z_distance_str = self._format_distance(stats['z_distance'])
            total_distance_str = self._format_distance(stats['total_distance'])
            extrusion_distance_str = self._format_distance(stats['extrusion_distance'])
            travel_distance_str = self._format_distance(stats['travel_distance'])
            filament_str = self._format_distance(stats['filament_used'])
            
            message = "<b>Analysis: " + str(source) + "</b><br><br>"
            message += "<table style='width:100%; border-collapse: collapse; font-family: monospace;'>"
            
            # Section Mouvements
            message += "<tr style='background-color: #f0f0f0;'><td colspan='2'><b>Movements</b></td></tr>"
            message += f"<tr><td>X moves:</td><td align='right'>{stats['x_moves']:,}".replace(',', ' ') + "</td></tr>"
            message += f"<tr><td>Y moves:</td><td align='right'>{stats['y_moves']:,}".replace(',', ' ') + "</td></tr>"
            message += f"<tr><td>Z moves (layers):</td><td align='right'>{stats['z_moves']:,}".replace(',', ' ') + "</td></tr>"
            message += f"<tr><td>Extrusion moves:</td><td align='right'>{stats['extrusion_moves']:,}".replace(',', ' ') + "</td></tr>"
            message += f"<tr><td>Travel moves:</td><td align='right'>{stats['travel_moves']:,}".replace(',', ' ') + "</td></tr>"
            
            # Section Distances
            message += "<tr style='background-color: #f0f0f0;'><td colspan='2'><b>Distances</b></td></tr>"
            message += f"<tr><td>X distance:</td><td align='right'>{x_distance_str}</td></tr>"
            message += f"<tr><td>Y distance:</td><td align='right'>{y_distance_str}</td></tr>"
            message += f"<tr><td>Z distance (height):</td><td align='right'>{z_distance_str}</td></tr>"
            message += f"<tr><td>Total distance:</td><td align='right'>{total_distance_str}</td></tr>"
            message += f"<tr><td>Extrusion distance:</td><td align='right'>{extrusion_distance_str}</td></tr>"
            message += f"<tr><td>Travel distance:</td><td align='right'>{travel_distance_str}</td></tr>"
            
            # Section Filament
            message += "<tr style='background-color: #f0f0f0;'><td colspan='2'><b>Filament</b></td></tr>"
            message += f"<tr><td>Filament used:</td><td align='right'>{filament_str}</td></tr>"
            message += f"<tr><td>Extrusion ratio:</td><td align='right'>{stats['extrusion_ratio']:.1f}%</td></tr>"
            
            # Section Vitesses
            message += "<tr style='background-color: #f0f0f0;'><td colspan='2'><b>Speeds</b></td></tr>"
            message += f"<tr><td>Min speed:</td><td align='right'>{stats['speed_min']:.1f} mm/s</td></tr>"
            message += f"<tr><td>Max speed:</td><td align='right'>{stats['speed_max']:.1f} mm/s</td></tr>"
            message += f"<tr><td>Average speed:</td><td align='right'>{stats['speed_avg']:.1f} mm/s</td></tr>"
            
            message += "</table>"
            
            msg_box = QMessageBox()
            msg_box.setWindowTitle("GCode Analysis Results")
            msg_box.setTextFormat(Qt.TextFormat.RichText)
            msg_box.setText(message)
            msg_box.setIcon(QMessageBox.Icon.Information)
            msg_box.exec()
            
            Logger.log("i", "Analysis completed: " + str(stats))
        except Exception as e:
            Logger.log("e", "Error displaying results: " + str(e))
    
    def _display_error(self, title, message):
        try:
            msg_box = QMessageBox()
            msg_box.setWindowTitle(str(title))
            msg_box.setText(str(message))
            msg_box.setIcon(QMessageBox.Icon.Warning)
            msg_box.exec()
        except Exception as e:
            Logger.log("e", "Error displaying error message: " + str(e))


def getMetaData():
    return {}


def register(app):
    return {
        "extension": GCodeAnalyzer(app)
    }