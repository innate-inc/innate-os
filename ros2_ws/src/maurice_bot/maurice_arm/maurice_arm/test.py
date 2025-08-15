#!/usr/bin/env python3
import os, re, math, tempfile, numpy as np, xml.etree.ElementTree as ET
import rclpy
from rclpy.node import Node
from sensor_msgs.msg import JointState
from ament_index_python.packages import get_package_share_directory
from urdf_parser_py.urdf import URDF, Mesh as URDFMesh
from maurice_arm.urdf import treeFromUrdfModel  # your local URDF→KDL parser
import PyKDL as kdl
import trimesh
import fcl

# ------------------ CONFIG ------------------
URDF_PATH   = "/home/vignesh/maurice-prod/ros2_ws/src/maurice_bot/maurice_sim/urdf/maurice.urdf"
BASE_LINK   = "base_link"
LINK_A_NAME = "base_link"
LINK_B_NAME = "link5"
JOINT_TOPIC = "/maurice_arm/state"
TIMER_HZ    = 1.0
# -------------------------------------------

def resolve_package_uris(text: str) -> str:
    def repl(m):
        pkg, rest = m.group(1), m.group(2)
        try:
            base = get_package_share_directory(pkg)
        except Exception:
            return m.group(0)
        return os.path.join(base, rest.lstrip('/')).replace('\\', '/')
    return re.sub(r'package://([A-Za-z0-9_\-]+)/([^"\'\s<]+)', repl, text)

def rpy_to_rot(r, p, y):
    cr, sr = math.cos(r), math.sin(r)
    cp, sp = math.cos(p), math.sin(p)
    cy, sy = math.cos(y), math.sin(y)
    # URDF uses extrinsic XYZ (roll,pitch,yaw) == intrinsic ZYX
    return np.array([
        [cy*cp, cy*sp*sr - sy*cr, cy*sp*cr + sy*sr],
        [sy*cp, sy*sp*sr + cy*cr, sy*sp*cr - cy*sr],
        [ -sp ,        cp*sr    ,        cp*cr    ],
    ], dtype=np.float64)

def mat4_from_xyz_rpy(xyz, rpy):
    T = np.eye(4, dtype=np.float64)
    T[:3, :3] = rpy_to_rot(rpy[0], rpy[1], rpy[2])
    T[:3, 3]  = xyz
    return T

def transform_vertices(V, T4):
    Vh = np.c_[V, np.ones((V.shape[0], 1), dtype=np.float64)]
    return (T4 @ Vh.T).T[:, :3]

class KDLFCLDistanceNode(Node):
    def __init__(self):
        super().__init__('kdl_fcl_distance_node')

        # -------- Load URDF (resolved paths) --------
        with open(URDF_PATH, 'r') as f:
            resolved = resolve_package_uris(f.read())
        tmp_dir = tempfile.mkdtemp(prefix='urdf_resolved_')
        self.urdf_resolved_path = os.path.join(tmp_dir, 'robot_resolved.urdf')
        with open(self.urdf_resolved_path, 'w') as f:
            f.write(resolved)

        self.robot_model = URDF.from_xml_file(self.urdf_resolved_path)

        # -------- Build KDL tree --------
        ok, self.kdl_tree = treeFromUrdfModel(self.robot_model)
        if not ok or self.kdl_tree is None:
            raise RuntimeError("Failed to build KDL tree from URDF")

        # Chains from base to each link (handle base→base as identity)
        self.chain_A = self._make_chain(BASE_LINK, LINK_A_NAME)
        self.chain_B = self._make_chain(BASE_LINK, LINK_B_NAME)

        self.fk_A = kdl.ChainFkSolverPos_recursive(self.chain_A) if self.chain_A is not None else None
        self.fk_B = kdl.ChainFkSolverPos_recursive(self.chain_B) if self.chain_B is not None else None

        # Joint name order for each chain (for building JntArray)
        self.joint_names_A = self._chain_joint_names(self.chain_A)
        self.joint_names_B = self._chain_joint_names(self.chain_B)

        # -------- Build FCL BVHs for the two links --------
        self._mesh_cache = {}
        
        print("=== BUILDING COLLISION MODELS ===")
        # Create BVH models first, then collision objects
        self.bvh_A = self._build_link_bvh(LINK_A_NAME)
        self.bvh_B = self._build_link_bvh(LINK_B_NAME)
        # Also build link2 for visualization
        self.bvh_link2 = self._build_link_bvh("link2")
        print("=== COLLISION MODELS BUILT ===")
        
        # Export collision meshes as STL files for visualization
        print("=== EXPORTING FCL MESHES ===")
        self.export_fcl_meshes()
        print("=== EXPORT COMPLETE ===")
        
        # Create collision objects with identity transforms initially
        self.co_A = fcl.CollisionObject(self.bvh_A)
        self.co_B = fcl.CollisionObject(self.bvh_B)

        # -------- ROS plumbing --------
        self.latest_joint = None
        self.create_subscription(JointState, JOINT_TOPIC, self._on_joint, 10)
        self.create_timer(1.0 / TIMER_HZ, self._on_timer)

        self.get_logger().info(
            f"Ready. KDL FK + FCL distance: {LINK_A_NAME} ↔ {LINK_B_NAME} @ {TIMER_HZ:.1f} Hz"
        )

    # ---------- KDL helpers ----------
    def _make_chain(self, base: str, tip: str):
        if tip == base:
            return None  # identity
        try:
            return self.kdl_tree.getChain(base, tip)
        except Exception:
            raise ValueError(f"KDL chain not found: {base} -> {tip}")

    @staticmethod
    def _chain_joint_names(chain: kdl.Chain | None):
        if chain is None:
            return []
        names = []
        for i in range(chain.getNrOfSegments()):
            seg = chain.getSegment(i)
            j = seg.getJoint()
            if j.getType() != kdl.Joint.Fixed:
                names.append(j.getName())
        return names

    def _jntarray_from_jointstate(self, chain_names, joint_msg: JointState):
        qa = kdl.JntArray(len(chain_names))
        if not chain_names:
            return qa  # size 0 for identity
        name_to_pos = dict(zip(joint_msg.name, joint_msg.position))
        for i, name in enumerate(chain_names):
            qa[i] = float(name_to_pos.get(name, 0.0))
        return qa

    @staticmethod
    def _frame_to_mat4(F: kdl.Frame):
        R = np.array([[F.M[0,0], F.M[0,1], F.M[0,2]],
                      [F.M[1,0], F.M[1,1], F.M[1,2]],
                      [F.M[2,0], F.M[2,1], F.M[2,2]]], dtype=np.float64)
        T = np.eye(4, dtype=np.float64)
        T[:3, :3] = R
        T[:3, 3]  = [F.p[0], F.p[1], F.p[2]]
        return T

    def export_fcl_meshes(self):
        """Export FCL collision objects as STL files for visualization."""
        
        # Extract vertices and faces from FCL BVH models
        for name in [LINK_A_NAME, LINK_B_NAME, "link2"]:
            try:
                link = next((l for l in self.robot_model.links if l.name == name), None)
                if link and link.collisions:
                    for i, col in enumerate(link.collisions):
                        if isinstance(col.geometry, URDFMesh):
                            V, F = self._load_mesh(col.geometry.filename)
                            
                            # Apply same transforms as in _build_link_bvh
                            scale = np.array(col.geometry.scale if col.geometry.scale else [1,1,1])
                            V = V * scale.reshape(1,3)
                            
                            xyz = np.array(col.origin.xyz if col.origin else [0,0,0])
                            rpy = np.array(col.origin.rpy if col.origin else [0,0,0])
                            T_local = mat4_from_xyz_rpy(xyz, rpy)
                            V = transform_vertices(V, T_local)
                            
                            # Create trimesh and save
                            mesh = trimesh.Trimesh(vertices=V, faces=F)
                            filename = f"fcl_{name}_collision_{i}.stl"
                            mesh.export(filename)
                            print(f"Exported {filename}")
                            
                            # Also print some info about the exported mesh
                            print(f"  Vertices: {V.shape[0]}, Faces: {F.shape[0]}")
                            print(f"  Bounds: {np.min(V, axis=0)} to {np.max(V, axis=0)}")
                            
            except Exception as e:
                print(f"Failed to export {name}: {e}")

    def export_transformed_meshes(self, T_A, T_B):
        """Export collision meshes in their current world transforms."""
        
        # Compute FK for link2 as well
        chain_link2 = self._make_chain(BASE_LINK, "link2")
        if chain_link2 is None:
            T_link2 = np.eye(4, dtype=np.float64)
        else:
            fk_link2 = kdl.ChainFkSolverPos_recursive(chain_link2)
            joint_names_link2 = self._chain_joint_names(chain_link2)
            q_link2 = self._jntarray_from_jointstate(joint_names_link2, self.latest_joint)
            F_link2 = kdl.Frame()
            rc_link2 = fk_link2.JntToCart(q_link2, F_link2)
            if rc_link2 < 0:
                T_link2 = np.eye(4, dtype=np.float64)
            else:
                T_link2 = self._frame_to_mat4(F_link2)
        
        # Include all links with their transforms
        transforms = {
            "base_link": np.eye(4, dtype=np.float64),  # Base link is at world origin
            LINK_A_NAME: T_A, 
            "link2": T_link2,
            LINK_B_NAME: T_B
        }
        
        for name, T in transforms.items():
            try:
                link = next((l for l in self.robot_model.links if l.name == name), None)
                if link and link.collisions:
                    for i, col in enumerate(link.collisions):
                        if isinstance(col.geometry, URDFMesh):
                            V, F = self._load_mesh(col.geometry.filename)
                            
                            # Apply local transforms (same as in _build_link_bvh)
                            scale = np.array(col.geometry.scale if col.geometry.scale else [1,1,1])
                            V = V * scale.reshape(1,3)
                            
                            xyz = np.array(col.origin.xyz if col.origin else [0,0,0])
                            rpy = np.array(col.origin.rpy if col.origin else [0,0,0])
                            T_local = mat4_from_xyz_rpy(xyz, rpy)
                            V = transform_vertices(V, T_local)
                            
                            # Apply world transform (FK result)
                            V = transform_vertices(V, T)
                            
                            # Create and save mesh
                            mesh = trimesh.Trimesh(vertices=V, faces=F)
                            filename = f"world_{name}_collision_{i}.stl"
                            mesh.export(filename)
                            print(f"Exported world position: {filename}")
                            print(f"  World bounds: {np.min(V, axis=0)} to {np.max(V, axis=0)}")
                            
            except Exception as e:
                print(f"Failed to export world {name}: {e}")

    # ---------- URDF mesh → FCL BVH (separated from CollisionObject creation) ----------
    def _build_link_bvh(self, link_name: str):
        print(f"\n--- Building BVH for {link_name} ---")
        # find link
        link = next((l for l in self.robot_model.links if l.name == link_name), None)
        if link is None:
            self.get_logger().warn(f"Link '{link_name}' not in URDF; creating empty BVH.")
            bvh = fcl.BVHModel()
            bvh.beginModel(0, 0)
            bvh.endModel()
            return bvh

        all_V, all_F, v_ofs = [], [], 0
        if not link.collisions:
            self.get_logger().warn(f"Link '{link_name}' has no <collision> geometry; using empty model.")
        else:
            for col in link.collisions:
                geom = col.geometry
                if not isinstance(geom, URDFMesh):
                    # (optional) handle primitives: box/sphere/cylinder → FCL primitives
                    continue
                filename = geom.filename
                scale = np.array(geom.scale if geom.scale is not None else [1,1,1], dtype=np.float64)

                V, F = self._load_mesh(filename)
                print(f"Raw mesh bounds for {link_name}: {np.min(V, axis=0)} to {np.max(V, axis=0)}")
                
                # Apply mesh scale from URDF
                V = V * scale.reshape(1,3)
                print(f"After scale: {np.min(V, axis=0)} to {np.max(V, axis=0)}")

                # origin is urdf_parser_py.Pose(xyz, rpy)
                xyz = np.array(col.origin.xyz if col.origin else [0,0,0], dtype=np.float64)
                rpy = np.array(col.origin.rpy if col.origin else [0,0,0], dtype=np.float64)
                print(f"Collision origin offset: xyz={xyz}, rpy={rpy}")
                
                T_local = mat4_from_xyz_rpy(xyz, rpy)
                V = transform_vertices(V, T_local)
                print(f"After local transform: {np.min(V, axis=0)} to {np.max(V, axis=0)}")

                all_V.append(V)
                all_F.append(F + v_ofs)
                v_ofs += V.shape[0]

        if not all_V:
            bvh = fcl.BVHModel()
            bvh.beginModel(0, 0)
            bvh.endModel()
            return bvh

        V_all = np.vstack(all_V).astype(np.float64, copy=False)
        F_all = np.vstack(all_F).astype(np.int32, copy=False)
        
        print(f"Final {link_name} collision mesh bounds: {np.min(V_all, axis=0)} to {np.max(V_all, axis=0)}")
        
        # Check distance from origin
        min_dist_to_origin = np.min(np.linalg.norm(V_all, axis=1))
        print(f"Minimum distance from {link_name} collision geometry to origin: {min_dist_to_origin:.6f} m")

        bvh = fcl.BVHModel()
        bvh.beginModel(V_all.shape[0], F_all.shape[0])
        bvh.addSubModel(V_all, F_all)
        bvh.endModel()

        return bvh

    # ---------- URDF mesh → FCL ----------
    def _build_link_fcl(self, link_name: str):
        # find link
        link = next((l for l in self.robot_model.links if l.name == link_name), None)
        if link is None:
            self.get_logger().warn(f"Link '{link_name}' not in URDF; creating empty FCL object.")
            bvh = fcl.BVHModel(); bvh.beginModel(0, 0); bvh.endModel()
            return fcl.CollisionObject(bvh, np.eye(4))

        all_V, all_F, v_ofs = [], [], 0
        if not link.collisions:
            self.get_logger().warn(f"Link '{link_name}' has no <collision> geometry; using empty model.")
        else:
            for col in link.collisions:
                geom = col.geometry
                if not isinstance(geom, URDFMesh):
                    # (optional) handle primitives: box/sphere/cylinder → FCL primitives
                    continue
                filename = geom.filename
                scale = np.array(geom.scale if geom.scale is not None else [1,1,1], dtype=np.float64)

                V, F = self._load_mesh(filename)
                V = V * scale.reshape(1,3)

                # origin is urdf_parser_py.Pose(xyz, rpy)
                xyz = np.array(col.origin.xyz if col.origin else [0,0,0], dtype=np.float64)
                rpy = np.array(col.origin.rpy if col.origin else [0,0,0], dtype=np.float64)
                T_local = mat4_from_xyz_rpy(xyz, rpy)
                V = transform_vertices(V, T_local)

                all_V.append(V)
                all_F.append(F + v_ofs)
                v_ofs += V.shape[0]

        if not all_V:
            bvh = fcl.BVHModel(); bvh.beginModel(0, 0); bvh.endModel()
            return fcl.CollisionObject(bvh, np.eye(4))

        V_all = np.vstack(all_V).astype(np.float64, copy=False)
        F_all = np.vstack(all_F).astype(np.int32, copy=False)

        bvh = fcl.BVHModel()
        bvh.beginModel(V_all.shape[0], F_all.shape[0])
        bvh.addSubModel(V_all, F_all)
        bvh.endModel()

        return fcl.CollisionObject(bvh, np.eye(4))

    def _load_mesh(self, filename: str):
        if not hasattr(self, "_mesh_cache"):
            self._mesh_cache = {}
        if filename in self._mesh_cache:
            return self._mesh_cache[filename]
        m = trimesh.load(filename, force='mesh')
        if not isinstance(m, trimesh.Trimesh):
            m = trimesh.util.concatenate(m.dump())
        V = np.asarray(m.vertices, dtype=np.float64)
        F = np.asarray(m.faces, dtype=np.int32)
        
        # Debug: Print mesh bounds
        bounds = m.bounds
        size = bounds[1] - bounds[0]
        print(f"Mesh {filename}:")
        print(f"  Bounds: {bounds}")
        print(f"  Size: {size}")
        print(f"  Max dimension: {np.max(size):.4f}")
        
        self._mesh_cache[filename] = (V, F)
        return V, F

    def get_true_distance(self, co_A, co_B):
        """Get the true distance between two collision objects, handling FCL's collision detection quirks."""
        
        # Try distance query first
        req = fcl.DistanceRequest()
        req.enable_nearest_points = True
        req.enable_signed_distance = True
        req.rel_err = 0.0
        req.abs_err = 0.0
        
        res = fcl.DistanceResult()
        
        try:
            fcl_dist = fcl.distance(co_A, co_B, req, res)
        except Exception:
            return None, None, None
            
        # Check for collision
        col_req = fcl.CollisionRequest()
        col_res = fcl.CollisionResult()
        is_colliding = fcl.collide(co_A, co_B, col_req, col_res)
        
        # Get nearest points
        nearest_points = None
        manual_dist = None
        if hasattr(res, 'nearest_points') and res.nearest_points:
            pa, pb = res.nearest_points
            nearest_points = (pa, pb)
            manual_dist = np.linalg.norm(pa - pb)
        
        # Determine the true distance
        if is_colliding and fcl_dist == 0.0 and manual_dist is not None:
            # FCL thinks they're colliding but nearest points show they're close
            true_dist = manual_dist
            status = "FCL_COLLISION_OVERRIDE"
        elif fcl_dist < 0.0:
            # True penetration
            true_dist = -fcl_dist
            status = "PENETRATION"
        else:
            # Normal case
            true_dist = fcl_dist
            status = "NORMAL"
            
        return true_dist, nearest_points, status

    # ---------- ROS callbacks ----------
    def _on_joint(self, msg: JointState):
        self.latest_joint = msg

    def _on_timer(self):
        if self.latest_joint is None:
            return

        # FK for link A
        if self.chain_A is None:  # base → base
            T_A = np.eye(4, dtype=np.float64)
        else:
            qA = self._jntarray_from_jointstate(self.joint_names_A, self.latest_joint)
            F_A = kdl.Frame()
            rcA = self.fk_A.JntToCart(qA, F_A)
            if rcA < 0:
                self.get_logger().warn(f"FK failed for {LINK_A_NAME} (rc={rcA}); using identity.")
                T_A = np.eye(4, dtype=np.float64)
            else:
                T_A = self._frame_to_mat4(F_A)

        # FK for link B
        if self.chain_B is None:
            T_B = np.eye(4, dtype=np.float64)
        else:
            qB = self._jntarray_from_jointstate(self.joint_names_B, self.latest_joint)
            F_B = kdl.Frame()
            rcB = self.fk_B.JntToCart(qB, F_B)
            if rcB < 0:
                self.get_logger().warn(f"FK failed for {LINK_B_NAME} (rc={rcB}); using identity.")
                T_B = np.eye(4, dtype=np.float64)
            else:
                T_B = self._frame_to_mat4(F_B)

        # Debug: Print link origins (translation parts of transforms)
        origin_A = T_A[:3, 3]
        origin_B = T_B[:3, 3]
        origin_dist = np.linalg.norm(origin_B - origin_A)
        
        print(f"Link origins distance: {origin_dist:.4f} m")
        print(f"  {LINK_A_NAME} origin: {np.array2string(origin_A, precision=4)}")
        print(f"  {LINK_B_NAME} origin: {np.array2string(origin_B, precision=4)}")

        # Update FCL transforms
        try:
            self.co_A.setTransformation(T_A)
            self.co_B.setTransformation(T_B)
        except AttributeError:
            self.co_A.setTransform(T_A)
            self.co_B.setTransform(T_B)

        # Export world-positioned meshes once for visualization
        if not hasattr(self, '_exported_world'):
            print("=== EXPORTING WORLD-POSITIONED MESHES ===")
            self.export_transformed_meshes(T_A, T_B)
            print("=== WORLD EXPORT COMPLETE ===")
            self._exported_world = True

        # Get true distance using our custom function
        true_dist, nearest_points, status = self.get_true_distance(self.co_A, self.co_B)
        
        if true_dist is not None:
            print(f"Surface distance: {true_dist:.6f} m ({status})")
            
            if nearest_points:
                pa, pb = nearest_points
                print(f"  {LINK_A_NAME} surface: {np.array2string(pa, precision=4)}")
                print(f"  {LINK_B_NAME} surface: {np.array2string(pb, precision=4)}")
        else:
            print(f"[ERROR] Failed to compute distance")
        
        print("---")

def main():
    rclpy.init()
    node = KDLFCLDistanceNode()
    try:
        rclpy.spin(node)  # single-threaded: one sub + one timer
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()

if __name__ == '__main__':
    main()
